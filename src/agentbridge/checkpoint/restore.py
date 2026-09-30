"""Validate native state privately, then install it into an execution-held restored Store."""

import json
import os
from pathlib import Path
import shutil
import tempfile

from . import content, native, persistence, state


def restore(store, *, format_version, operation_id, params):
    state.canonical_uuid(operation_id)
    if (format_version != '1' or not isinstance(params, dict)
            or set(params) != {'checkpoint', 'destination_generation', 'content'}):
        native.fail('checkpoint_invalid')
    descriptor, supplied = params['checkpoint'], params['content']
    if not isinstance(descriptor, dict) or supplied != descriptor.get('content'):
        native.fail('checkpoint_corrupt')
    instance_id = descriptor['instance_id']
    payload = json.dumps(params, sort_keys=True, separators=(',', ':'))
    with content.instance_lock(store, instance_id):
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            binding = state.identity(db)
            if (not state.recovery_held(db, instance_id) or not binding['enabled']
                    or params['destination_generation'] != binding['store_generation']
                    or descriptor['owner_ref'] != binding['owner_ref']
                    or descriptor['store_id'] != binding['store_id']
                    or descriptor['store_generation'] != binding['source_generation']):
                native.fail('checkpoint_scope_mismatch')
            historical = persistence.record(db, descriptor['turn_id'])
            if (not historical or historical['descriptor'] is None
                    or json.loads(historical['descriptor']) != descriptor
                    or historical['instance_id'] != instance_id):
                native.fail('checkpoint_scope_mismatch')
            session = db.execute('SELECT * FROM sessions WHERE id=?', (instance_id,)).fetchone()
            if not session or session['native_id'] != descriptor['native_id']:
                native.fail('checkpoint_scope_mismatch')
            latest = db.execute('SELECT id FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1',
                                (instance_id,)).fetchone()
            if not latest or latest[0] != descriptor['turn_id']:
                native.fail('checkpoint_incomplete')
            operation = db.execute('SELECT * FROM checkpoint_restore_operations WHERE operation_id=?',
                                   (operation_id,)).fetchone()
            if operation and operation['payload'] != payload:
                native.fail('checkpoint_scope_mismatch')
            db.execute('INSERT OR IGNORE INTO checkpoint_restore_operations '
                       'VALUES (?, ?, ?, ?, ?, NULL)',
                       (operation_id, payload, 'preparing', instance_id, descriptor['checkpoint_id']))
        home = store.root / 'codex-runtime' / instance_id
        parent = native.private_directory(home.parent)
        # Retrying an interrupted publication checks a private install marker below the home.
        marker = home / '.agentbridge-checkpoint.json'
        if home.exists():
            if home.is_symlink() or not marker.is_file() or marker.is_symlink():
                native.fail('checkpoint_incomplete')
            installed = json.loads(marker.read_text())
            if installed != {'operation_id': operation_id, 'checkpoint_id': descriptor['checkpoint_id']}:
                native.fail('checkpoint_scope_mismatch')
            expected = native.fingerprint(home)
            if operation is None or json.loads(operation['result'] or '{}').get('fingerprint') != expected:
                native.fail('checkpoint_corrupt')
        else:
            stage = Path(tempfile.mkdtemp(prefix='.restore-native-', dir=parent))
            try:
                content.extract(store, descriptor, stage)
                observed = native.runtime(store, dict(session), home=stage)
                if observed != descriptor['runtime']:
                    native.fail('checkpoint_incompatible')
                with store.connect() as db:
                    workspace = db.execute('SELECT * FROM recovery_workspaces WHERE instance_id=?',
                                           (instance_id,)).fetchone()
                native.relocate_indexes(stage, descriptor['native_id'], target_home=home,
                                        workspace=dict(workspace) if workspace else None)
                marker_payload = {'operation_id': operation_id,
                                  'checkpoint_id': descriptor['checkpoint_id']}
                staged_marker = stage / marker.name
                fd = os.open(staged_marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'w') as out:
                    json.dump(marker_payload, out)
                    out.flush()
                    os.fsync(out.fileno())
                expected = native.fingerprint(stage)
                with store.connect() as db:
                    db.execute('UPDATE checkpoint_restore_operations SET state=?,result=? '
                               'WHERE operation_id=?',
                               ('publishing', json.dumps({'fingerprint': expected}), operation_id))
                content.sync_tree(stage)
                os.rename(stage, home)
                content.sync_directory(parent)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        result = {'format_version': '1', 'state': 'restored_held', 'instance_id': instance_id,
                  'turn_id': descriptor['turn_id'], 'native_id': descriptor['native_id'],
                  'checkpoint_id': descriptor['checkpoint_id'], 'fingerprint': expected,
                  'store_generation': binding['store_generation']}
        with store.connect() as db:
            db.execute('UPDATE checkpoint_restore_operations SET state=?,result=? WHERE operation_id=?',
                       ('done', json.dumps(result), operation_id))
        return result

