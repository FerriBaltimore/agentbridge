"""Transactional whole-Store SQLite snapshots and coverage read from the copied database."""

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from uuid import uuid4

from . import content, native, persistence, state


def coverage(db):
    rows = db.execute('''SELECT c.*,s.native_id FROM native_checkpoints c
        JOIN sessions s ON s.id=c.instance_id
        JOIN runs r ON r.id=c.turn_id
        WHERE c.state='ready'
        AND NOT EXISTS(SELECT 1 FROM continuity_holds h WHERE h.instance_id=c.instance_id)
        AND (SELECT recovery_held FROM store_identity WHERE singleton=1)=0
        AND c.generation=(SELECT store_generation FROM store_identity WHERE singleton=1)
        AND r.state IN
          ('completed','failed','interrupted','incomplete','cancelled')
        AND NOT EXISTS(SELECT 1 FROM runs later
                       WHERE later.session_id=r.session_id AND later.rowid>r.rowid)
        ORDER BY c.instance_id''').fetchall()
    result = []
    for row in rows:
        descriptor = json.loads(row['descriptor'])
        terminal = db.execute('SELECT kind FROM events WHERE seq=? AND run_id=?',
                              (row['terminal_seq'], row['turn_id'])).fetchone()
        observed = db.execute('SELECT data FROM events WHERE seq=? AND kind=?',
                              (row['observation_seq'], 'checkpoint_ready')).fetchone()
        if (not terminal or terminal[0] != 'run_finished' or not observed
                or descriptor['native_id'] != row['native_id']
                or json.loads(observed[0])['checkpoint'] != descriptor
                or not row['execution_seq'] < row['terminal_seq'] < row['observation_seq']):
            native.fail('checkpoint_corrupt')
        result.append({'instance_id': row['instance_id'], 'native_id': row['native_id'],
                       'turn_id': row['turn_id'], 'checkpoint_id': row['checkpoint_id'],
                       'execution_seq': row['execution_seq'], 'terminal_seq': row['terminal_seq'],
                       'checkpoint_event_seq': row['observation_seq']})
    return result


def inspect(path, *, expected=None):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            native.fail('checkpoint_corrupt')
        binding = state.identity(db)
        if expected and any(binding[key] != expected[key] for key in state.IDENTITY_KEYS):
            native.fail('checkpoint_scope_mismatch')
        return {'format_version': '1', **{key: binding[key] for key in state.IDENTITY_KEYS},
                'backend': 'sqlite', 'store_schema': db.execute('SELECT version FROM metadata')
                .fetchone()[0], 'cursor': state.cursor(db), 'coverage': coverage(db)}


def snapshot_store(store, *, format_version, operation_id, params):
    state.canonical_uuid(operation_id)
    if format_version != '1' or not isinstance(params, dict) or set(params) != set(state.IDENTITY_KEYS):
        native.fail('checkpoint_invalid')
    with content.instance_lock(store, operation_id):
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            binding = state.identity(db)
            if not binding['enabled'] or any(params[key] != binding[key] for key in state.IDENTITY_KEYS):
                native.fail('checkpoint_scope_mismatch')
            if store.storage.name == 'postgresql':
                native.fail('checkpoint_sql_backup_required')
            operation = db.execute('SELECT * FROM store_snapshot_operations WHERE operation_id=?',
                                   (operation_id,)).fetchone()
            if operation and operation['generation'] != binding['store_generation']:
                native.fail('checkpoint_scope_mismatch')
            if operation and operation['descriptor']:
                descriptor = json.loads(operation['descriptor'])
            created_at = operation['created_at'] if operation else persistence.now()
            db.execute('INSERT OR IGNORE INTO store_snapshot_operations VALUES (?,?,?,NULL)',
                       (operation_id, binding['store_generation'], created_at))
        if operation and operation['descriptor']:
            content.resolve(store, descriptor['content'])
            return descriptor
        target = content.object_path(store, operation_id)
        if not target.exists():
            fd, temporary = tempfile.mkstemp(prefix='.sqlite-', dir=content.directory(store))
            os.close(fd)
            staged = Path(temporary)
            try:
                deadline = time.monotonic() + 120

                def bounded(status, remaining, total):
                    if time.monotonic() >= deadline:
                        native.fail('checkpoint_busy')

                with store.connect() as origin, closing(sqlite3.connect(staged)) as destination, destination:
                    origin.backup(destination, pages=128, progress=bounded, sleep=.01)
                reference = content.publish(store, operation_id, staged)
            finally:
                staged.unlink(missing_ok=True)
        else:
            checksum, size = native.digest(target)
            reference = {'content_id': operation_id, 'sha256': checksum, 'bytes': size}
        descriptor = {**inspect(target, expected=binding), 'snapshot_id': operation_id,
                      'created_at': created_at, 'content': reference}
        with store.connect() as db:
            db.execute('UPDATE store_snapshot_operations SET descriptor=? WHERE operation_id=?',
                       (json.dumps(descriptor), operation_id))
        return descriptor


def restore_store(destination, snapshot, source, *, owner_ref, workspace_paths=None):
    """Trusted host import into a new Store root; execution remains persistently held.

    The source is an authenticated/decrypted local object. No machine credentials are loaded,
    no proxy starts, and restored process IDs never establish live execution authority.
    """
    destination, source = Path(destination).absolute(), Path(source).absolute()
    if destination.exists() or destination.is_symlink() or owner_ref != snapshot['owner_ref']:
        native.fail('checkpoint_scope_mismatch')
    reference = snapshot['content']
    if native.digest(source) != (reference['sha256'], reference['bytes']):
        native.fail('checkpoint_corrupt')
    observed = inspect(source, expected=snapshot)
    if (any(observed[key] != snapshot[key] for key in observed)
            or snapshot['store_schema'] not in {14, 15} or snapshot['backend'] != 'sqlite'):
        native.fail('checkpoint_incompatible')
    parent = destination.parent
    if any(path.is_symlink() for path in (parent, *parent.parents)):
        native.fail('checkpoint_unsafe_path')
    if not parent.exists():
        native.private_directory(parent)
    if not parent.is_dir():
        native.fail('checkpoint_unsafe_path')
    stage = Path(tempfile.mkdtemp(prefix='.store-restore-', dir=parent))
    generation = str(uuid4())
    try:
        target = stage / 'bridge.sqlite3'
        shutil.copyfile(source, target)
        target.chmod(0o600)
        with closing(sqlite3.connect(target)) as db, db:
            db.row_factory = sqlite3.Row
            from .upgrade import migrate

            # Only the reviewed 14 -> 15 table addition is allowed. Store identity and
            # replay positions are unchanged by this migration; restore changes generation
            # separately below, invalidating every imported upgrade proof.
            if migrate(db, snapshot['store_schema']) != 15:
                native.fail('checkpoint_incompatible')
            db.execute('UPDATE store_identity SET store_generation=?,source_generation=?,source_seq=?, '
                       'recovery_held=1,enabled=1 WHERE singleton=1',
                       (generation, snapshot['store_generation'], snapshot['cursor']['seq']))
            db.execute('UPDATE runs SET worker_pid=NULL,worker_identity=NULL,child_pid=NULL,'
                       'child_identity=NULL')
            db.execute('UPDATE conversation_queues SET dispatcher_pid=NULL,dispatcher_identity=NULL')
            from .workspaces import rebind

            db.execute('DELETE FROM recovery_workspaces')
            rebind(db, destination, workspace_paths)
            from .recovery import interrupt_copied_execution

            interrupt_copied_execution(db)
            from ..proxy.credential_barrier import reset_restored_authority

            reset_restored_authority(db, generation)
        with target.open('rb') as handle:
            os.fsync(handle.fileno())
        content.sync_directory(stage)
        os.rename(stage, destination)
        content.sync_directory(parent)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'store_id': snapshot['store_id'], 'store_generation': generation,
            'owner_ref': owner_ref, 'recovery_held': True}
