"""Bind a held restore to the authenticated native origin, never a newer session cwd."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from ..workspace_policy import validate_execution_workspace
from . import content, native, persistence, state


def binding(store, db, descriptor, params, operation_id):
    identity = state.identity(db)
    instance = descriptor['instance_id']
    if (not state.recovery_held(db, instance) or not identity['enabled']
            or params['destination_generation'] != identity['store_generation']
            or descriptor['owner_ref'] != identity['owner_ref']
            or descriptor['store_id'] != identity['store_id']
            or descriptor['store_generation'] != identity['source_generation']):
        native.fail('checkpoint_scope_mismatch')
    historical = persistence.record(db, descriptor['turn_id'])
    if (not historical or historical['descriptor'] is None
            or json.loads(historical['descriptor']) != descriptor
            or historical['instance_id'] != instance):
        native.fail('checkpoint_scope_mismatch')
    session = db.execute('SELECT * FROM sessions WHERE id=?', (instance,)).fetchone()
    latest = db.execute('SELECT id FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1',
                        (instance,)).fetchone()
    if (not session or session['native_id'] != descriptor['native_id']
            or not latest or latest[0] != descriptor['turn_id']):
        native.fail('checkpoint_scope_mismatch')
    operation = db.execute('SELECT payload FROM checkpoint_restore_operations WHERE operation_id=?',
                           (operation_id,)).fetchone()
    if operation and operation[0] != json.dumps(params, sort_keys=True, separators=(',', ':')):
        native.fail('checkpoint_scope_mismatch')
    workspace = db.execute('SELECT * FROM recovery_workspaces WHERE instance_id=?',
                           (instance,)).fetchone()
    if workspace is not None:
        target = Path(workspace['target_path'])
        if (str(target) != session['cwd'] or not target.is_absolute()
                or target != target.resolve() or not target.is_dir()
                or target.stat().st_uid != os.getuid() or target.stat().st_mode & 0o077):
            native.fail('checkpoint_unsafe_path')
        validate_execution_workspace(str(target), store.root, workspace_write=True)
    return dict(identity), dict(workspace) if workspace is not None else None


def native_origin(store, descriptor):
    # Use the existing complete capsule verifier. The private extraction is removed before
    # returning; the immutable capsule and every historical transcript remain unchanged.
    with tempfile.TemporaryDirectory(prefix='.workspace-restore-',
                                     dir=content.directory(store)) as folder:
        stage = Path(folder)
        content.extract(store, descriptor, stage)
        native.validate_indexes(stage, descriptor['native_id'])
        index = stage / 'state_5.sqlite'
        if not index.exists():
            return None
        # Captures contain a transactional SQLite backup, without live WAL companions.
        with closing(sqlite3.connect(index.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
            db.execute('PRAGMA trusted_schema=OFF')
            columns = {row[1] for row in db.execute('PRAGMA table_info(threads)')}
            if 'cwd' not in columns:
                return None
            rows = db.execute('SELECT id,cwd FROM threads').fetchmany(native.MAX_FILES + 1)
        if len(rows) > native.MAX_FILES:
            native.fail('checkpoint_incomplete')
        origins = [cwd for identifier, cwd in rows if identifier == descriptor['native_id']]
        if len(origins) != 1 or not isinstance(origins[0], str):
            native.fail('checkpoint_scope_mismatch')
        source = Path(origins[0])
        if not source.is_absolute() or '..' in source.parts:
            native.fail('checkpoint_scope_mismatch')
        for _, cwd in rows:
            if not isinstance(cwd, str):
                native.fail('checkpoint_scope_mismatch')
            path = Path(cwd)
            if '..' in path.parts or not path.is_relative_to(source):
                native.fail('checkpoint_scope_mismatch')
        return str(source)


def prepare(store, *, format_version, operation_id, params):
    state.canonical_uuid(operation_id)
    if (format_version != '1' or not isinstance(params, dict)
            or set(params) != {'checkpoint', 'destination_generation', 'content'}):
        native.fail('checkpoint_invalid')
    descriptor = params['checkpoint']
    if not isinstance(descriptor, dict) or params['content'] != descriptor.get('content'):
        native.fail('checkpoint_corrupt')
    with content.instance_lock(store, descriptor['instance_id']):
        with store.connect() as db:
            before = binding(store, db, descriptor, params, operation_id)
        origin = native_origin(store, descriptor)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if binding(store, db, descriptor, params, operation_id) != before:
                native.fail('checkpoint_scope_mismatch')
            if origin is not None and before[1] is not None:
                # The trusted destination is unchanged. Only the source used by the existing
                # strict relocation guard follows the capsule's bound principal native ID.
                db.execute('UPDATE recovery_workspaces SET source_path=? WHERE instance_id=?',
                           (origin, descriptor['instance_id']))
        return {'format_version': '1', 'state': 'workspace_prepared',
                'instance_id': descriptor['instance_id'],
                'checkpoint_id': descriptor['checkpoint_id'],
                'store_generation': before[0]['store_generation']}
