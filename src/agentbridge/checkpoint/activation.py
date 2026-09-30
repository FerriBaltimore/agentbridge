"""Host-only release of verified instances, preserving every unresolved recovery hold."""

from contextlib import ExitStack
import json

from ..errors import BridgeError
from ..native_sessions import require_native_session
from . import content, native, state


def verify_instance(store, db, binding, instance_id):
    session = db.execute('SELECT id,native_id FROM sessions WHERE id=?', (instance_id,)).fetchone()
    if not session:
        native.fail('checkpoint_scope_mismatch')
    unknown = db.execute("SELECT 1 FROM runs WHERE session_id=? "
                         "AND error='restore_unknown_outcome'", (instance_id,)).fetchone()
    if unknown:
        raise BridgeError('continuity_pending', 'Restored execution outcomes need reconciliation.')
    if session['native_id'] is None:
        require_native_session(db, session)
        return
    row = db.execute("SELECT result FROM checkpoint_restore_operations "
                     "WHERE instance_id=? AND state='done' ORDER BY rowid DESC LIMIT 1",
                     (instance_id,)).fetchone()
    if not row:
        native.fail('checkpoint_incomplete')
    result = json.loads(row[0])
    latest = db.execute('SELECT id FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1',
                        (instance_id,)).fetchone()
    if (result['store_generation'] != binding['store_generation'] or not latest
            or result['turn_id'] != latest[0] or result['native_id'] != session['native_id']):
        native.fail('checkpoint_incomplete')
    if native.fingerprint(store.root / 'codex-runtime' / instance_id) != result['fingerprint']:
        native.fail('checkpoint_corrupt')


def release_recovery(store, *, expected_generation, ready_instances=None):
    """Release all held instances, or only an explicit trusted-host selection.

    External effects, credentials and publication authority must already be reconciled by
    the host. This API checks local materialization and never resolves unknown history.
    Unselected instances remain fenced across restarts and further Store snapshots.
    """
    if ready_instances is not None and (
            not isinstance(ready_instances, list) or not ready_instances
            or any(not isinstance(value, str) for value in ready_instances)
            or len(set(ready_instances)) != len(ready_instances)):
        native.fail('checkpoint_invalid')
    with content.instance_lock(store, 'recovery-release'), ExitStack() as locks:
        with store.connect() as db:
            binding = state.identity(db)
            if not binding['enabled'] or expected_generation != binding['store_generation']:
                native.fail('checkpoint_scope_mismatch')
            all_instances = {row[0] for row in db.execute('SELECT id FROM sessions')}
            held = (all_instances if binding['recovery_held'] else
                    {row[0] for row in db.execute('SELECT instance_id FROM continuity_holds')})
            selected = held if ready_instances is None else set(ready_instances)
            if (not binding['recovery_held'] and not held) or not selected <= held:
                native.fail('checkpoint_scope_mismatch')
        for instance_id in sorted(selected):
            locks.enter_context(content.instance_lock(store, instance_id))
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if state.identity(db) != binding:
                native.fail('checkpoint_scope_mismatch')
            for instance_id in sorted(selected):
                verify_instance(store, db, binding, instance_id)
            if binding['recovery_held']:
                db.execute("INSERT OR IGNORE INTO continuity_holds "
                           "SELECT id,'native_recovery_pending' FROM sessions")
            db.executemany('DELETE FROM continuity_holds WHERE instance_id=?',
                           [(value,) for value in selected])
            db.execute('UPDATE store_identity SET recovery_held=0 WHERE singleton=1')
