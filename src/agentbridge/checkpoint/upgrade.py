"""Trusted-host transition from integer replay to durable, generation-scoped continuity.

The host must close and keep closed the complete execution boundary, commit each returned
binding in its own database, then confirm that commit. No operation is exposed over RPC.
"""

import json
import re
from uuid import UUID, uuid5

from ..native_sessions import require_native_session
from . import content, native, persistence, state


def migrate(db, version):
    if version != 14:
        return version
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    current = db.execute('SELECT version FROM metadata').fetchone()[0]
    if current != 14:
        return current
    db.execute('''CREATE TABLE IF NOT EXISTS checkpoint_upgrade_operations(
        operation_id TEXT PRIMARY KEY, generation TEXT NOT NULL UNIQUE, owner_ref TEXT NOT NULL,
        proof_ref TEXT NOT NULL, created_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS checkpoint_upgrade_instances(
        operation_id TEXT NOT NULL, instance_id TEXT NOT NULL, request TEXT NOT NULL,
        result TEXT, reconciliation_ref TEXT, PRIMARY KEY(operation_id,instance_id))''')
    db.execute('UPDATE metadata SET version=15')
    return 15


def operation(db, operation_id, *, require_enabled=True):
    row = db.execute('SELECT * FROM checkpoint_upgrade_operations WHERE operation_id=?',
                     (operation_id,)).fetchone()
    binding = state.identity(db)
    if (not row or row['generation'] != binding['store_generation']
            or row['owner_ref'] != binding['owner_ref']
            or require_enabled and not binding['enabled']):
        native.fail('checkpoint_scope_mismatch')
    return dict(row), binding


def allows_adoption(db, instance_id, operation_id):
    if operation_id is None:
        return False
    operation(db, operation_id)
    row = db.execute('SELECT reason FROM continuity_holds WHERE instance_id=?',
                     (instance_id,)).fetchone()
    return row is not None and row[0] == 'legacy_upgrade:' + operation_id


def verify(callback, scope):
    try:
        accepted = callable(callback) and callback(scope) is True
    except Exception:
        accepted = False
    if not accepted:
        native.fail('checkpoint_busy')


def begin(store, *, operation_id, owner_ref, proof_ref, verify_quiescence):
    state.canonical_uuid(operation_id)
    state.canonical_uuid(proof_ref)
    if not isinstance(owner_ref, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',
                                                        owner_ref):
        native.fail('checkpoint_scope_mismatch')
    with content.instance_lock(store, 'durable-upgrade'):
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            binding = state.identity(db)
            prior = db.execute('SELECT * FROM checkpoint_upgrade_operations WHERE operation_id=?',
                               (operation_id,)).fetchone()
            if prior:
                saved, binding = operation(db, operation_id, require_enabled=False)
                if saved['owner_ref'] != owner_ref or saved['proof_ref'] != proof_ref:
                    native.fail('checkpoint_scope_mismatch')
            else:
                if binding['enabled'] or binding['recovery_held']:
                    native.fail('checkpoint_scope_mismatch')
                # Persist every hold before proving quiescence or changing execution mode.
                # A failed proof leaves any old execution in legacy mode, behind this fence.
                db.execute('UPDATE store_identity SET owner_ref=?,recovery_held=1 '
                           'WHERE singleton=1', (owner_ref,))
                db.execute('INSERT INTO checkpoint_upgrade_operations VALUES (?,?,?,?,?)',
                           (operation_id, binding['store_generation'], owner_ref, proof_ref,
                            persistence.now()))
                db.execute('INSERT INTO continuity_holds SELECT id,? FROM sessions',
                           ('legacy_upgrade:' + operation_id,))
                binding = state.identity(db)
            scope = {**{key: binding[key] for key in state.IDENTITY_KEYS},
                     'operation_id': operation_id, 'proof_ref': proof_ref}
        verify(verify_quiescence, scope)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if state.identity(db) != binding:
                native.fail('checkpoint_scope_mismatch')
            # Existing instances each retain their hold. New, never-started instances may
            # use the now-durable path without waiting for unrelated legacy repairs.
            db.execute('UPDATE store_identity SET enabled=1,recovery_held=0 WHERE singleton=1')
        return scope


def expected_history(db, instance_id, expected):
    if (not isinstance(expected, dict)
            or set(expected) != {'turn_id', 'native_id', 'after_seq'}
            or type(expected['after_seq']) is not int or expected['after_seq'] < 0):
        native.fail('checkpoint_invalid')
    session = db.execute('SELECT * FROM sessions WHERE id=?', (instance_id,)).fetchone()
    if session is None or session['native_id'] != expected['native_id']:
        native.fail('checkpoint_scope_mismatch')
    require_native_session(db, session)
    run = db.execute('SELECT * FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1',
                     (instance_id,)).fetchone()
    if (run['id'] if run else None) != expected['turn_id']:
        native.fail('checkpoint_scope_mismatch')
    seq = expected['after_seq']
    if seq > state.maximum_seq(db):
        native.fail('cursor_rollback')
    if seq:
        event = db.execute('SELECT * FROM events WHERE seq=?', (seq,)).fetchone()
        if (not event or event['session_id'] != instance_id
                or event['run_id'] != expected['turn_id']):
            native.fail('checkpoint_scope_mismatch')
    if session['native_id'] is None:
        if run is not None or seq:
            native.fail('checkpoint_incomplete')
        return None
    state.canonical_uuid(session['native_id'])
    if not run or run['state'] not in {'completed', 'failed', 'cancelled', 'interrupted',
                                      'incomplete'} or run['error'] == 'restore_unknown_outcome':
        native.fail('checkpoint_busy')
    terminal = db.execute("SELECT seq FROM events WHERE run_id=? AND kind='run_finished' "
                          'ORDER BY seq DESC LIMIT 1', (run['id'],)).fetchone()
    if not terminal or seq < terminal[0]:
        # The host must reconcile the legacy terminal before attempting this transition.
        # This API neither synthesizes that result nor skips unconsumed execution events.
        native.fail('checkpoint_incomplete')
    return terminal[0]


def stage(store, instance_id, *, operation_id, expected, verify_quiescence):
    from .adoption import adopt_drained

    state.canonical_uuid(operation_id)
    encoded = json.dumps(expected, sort_keys=True, separators=(',', ':'))
    with content.instance_lock(store, 'durable-upgrade'):
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            saved, binding = operation(db, operation_id)
            prior = db.execute('SELECT * FROM checkpoint_upgrade_instances '
                               'WHERE operation_id=? AND instance_id=?',
                               (operation_id, instance_id)).fetchone()
            if prior and prior['request'] != encoded:
                native.fail('checkpoint_scope_mismatch')
            if prior and prior['result']:
                return json.loads(prior['result'])
            if not allows_adoption(db, instance_id, operation_id):
                native.fail('checkpoint_scope_mismatch')
            terminal_seq = expected_history(db, instance_id, expected)
            db.execute('INSERT OR IGNORE INTO checkpoint_upgrade_instances '
                       'VALUES (?,?,?,NULL,NULL)', (operation_id, instance_id, encoded))
            scope = {**{key: binding[key] for key in state.IDENTITY_KEYS},
                     'operation_id': operation_id, 'proof_ref': saved['proof_ref'],
                     'instance_id': instance_id}
        verify(verify_quiescence, scope)
        descriptor = None
        if terminal_seq is not None:
            descriptor = adopt_drained(store, instance_id,
                operation_id=str(uuid5(UUID(operation_id), instance_id)),
                proof_ref=saved['proof_ref'], verify_quiescence=verify_quiescence,
                _upgrade_operation=operation_id)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            operation(db, operation_id)
            expected_history(db, instance_id, expected)
            checkpoint = persistence.record(db, expected['turn_id']) if descriptor else None
            result = {'operation_id': operation_id, 'instance_id': instance_id,
                      'turn_id': expected['turn_id'], 'native_id': expected['native_id'],
                      'cursor': state.cursor(db, expected['after_seq']),
                      'terminal_seq': terminal_seq, 'checkpoint': descriptor,
                      'checkpoint_event_seq': checkpoint['observation_seq'] if checkpoint else None}
            db.execute('UPDATE checkpoint_upgrade_instances SET result=? '
                       'WHERE operation_id=? AND instance_id=?',
                       (json.dumps(result), operation_id, instance_id))
            return result


def confirm(store, instance_id, *, operation_id, reconciliation_ref, verify_reconciliation):
    state.canonical_uuid(reconciliation_ref)
    with content.instance_lock(store, 'durable-upgrade'), content.instance_lock(store, instance_id):
        with store.connect() as db:
            saved, binding = operation(db, operation_id)
            row = db.execute('SELECT * FROM checkpoint_upgrade_instances '
                             'WHERE operation_id=? AND instance_id=?',
                             (operation_id, instance_id)).fetchone()
            if not row or not row['result']:
                native.fail('checkpoint_incomplete')
            result = json.loads(row['result'])
            if row['reconciliation_ref']:
                if row['reconciliation_ref'] != reconciliation_ref:
                    native.fail('checkpoint_scope_mismatch')
                return result
            if not allows_adoption(db, instance_id, operation_id):
                native.fail('checkpoint_scope_mismatch')
            expected_history(db, instance_id, json.loads(row['request']))
        if result['checkpoint'] is not None:
            content.resolve(store, result['checkpoint']['content'])
            with store.connect() as db:
                checkpoint = persistence.record(db, result['turn_id'])
            if (checkpoint is None or checkpoint['state'] != 'ready'
                    or json.loads(checkpoint['descriptor']) != result['checkpoint']
                    or checkpoint['source_fingerprint'] != native.fingerprint(
                        store.root / 'codex-runtime' / instance_id)):
                native.fail('checkpoint_corrupt')
        verify(verify_reconciliation, {**result, 'reconciliation_ref': reconciliation_ref})
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if state.identity(db) != binding:
                native.fail('checkpoint_scope_mismatch')
            db.execute('UPDATE checkpoint_upgrade_instances SET reconciliation_ref=? '
                       'WHERE operation_id=? AND instance_id=?',
                       (reconciliation_ref, operation_id, instance_id))
            db.execute('DELETE FROM continuity_holds WHERE instance_id=? AND reason=?',
                       (instance_id, 'legacy_upgrade:' + operation_id))
            # Every other session already has its own persistent hold from begin(). New
            # sessions could not execute while the global initial fence was held.
            db.execute('UPDATE store_identity SET recovery_held=0 WHERE singleton=1')
        return result
