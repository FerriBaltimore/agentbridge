"""Host-only adoption of drained legacy history; a request flag cannot attest supervision."""

import json
from uuid import uuid4

from ..models import TERMINAL
from ..process import alive
from . import content, native, persistence, state


def adopt_drained(store, instance_id, *, operation_id, proof_ref, verify_quiescence):
    """The trusted host verifies its stopped execution boundary while admission is fenced.

    The callback receives the exact owner/store/generation/instance/turn/barrier binding. It
    must prove no prior native descendant can write (for example an emptied, isolated service
    cgroup), and must keep that boundary closed until this call returns. Missing old PIDs are
    insufficient. Only a proof reference is persisted. This method is intentionally not RPC.
    """
    state.canonical_uuid(operation_id)
    state.canonical_uuid(proof_ref)
    if not callable(verify_quiescence):
        native.fail('checkpoint_incomplete')
    with content.instance_lock(store, instance_id):
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            binding = state.identity(db)
            if not binding['enabled'] or state.recovery_held(db, instance_id):
                native.fail('checkpoint_scope_mismatch')
            session = db.execute('SELECT * FROM sessions WHERE id=?', (instance_id,)).fetchone()
            if not session:
                native.fail('checkpoint_scope_mismatch')
            state.canonical_uuid(session['native_id'])
            run = db.execute('SELECT * FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1',
                             (instance_id,)).fetchone()
            if not run or run['state'] not in TERMINAL:
                native.fail('checkpoint_busy')
            prior = persistence.record(db, run['id'])
            terminal = db.execute("SELECT seq FROM events WHERE run_id=? AND kind='run_finished' "
                                  'ORDER BY seq DESC LIMIT 1', (run['id'],)).fetchone()
            if not terminal:
                native.fail('checkpoint_incomplete')
            if prior and prior['generation'] != binding['store_generation']:
                if prior['state'] != 'ready' or prior['operation_id'] == operation_id:
                    native.fail('checkpoint_scope_mismatch')
                # Original checkpoint descriptors remain in immutable historical observations.
                # This row is the working association for the current Store generation.
                db.execute('DELETE FROM native_checkpoints WHERE turn_id=?', (run['id'],))
                prior = None
            if prior and prior['operation_id'] != operation_id:
                native.fail('checkpoint_scope_mismatch')
            if prior is None:
                if db.execute('SELECT 1 FROM native_checkpoints WHERE operation_id=?',
                              (operation_id,)).fetchone():
                    native.fail('checkpoint_scope_mismatch')
                execution = db.execute('SELECT COALESCE(MAX(seq),0) FROM events '
                                       'WHERE run_id=? AND seq<?',
                                       (run['id'], terminal[0])).fetchone()[0]
                intent = json.dumps({'state': run['state'], 'code': run['error'],
                                     'exit_code': run['exit_code']})
                db.execute('''INSERT INTO native_checkpoints
                    (turn_id,instance_id,barrier_id,checkpoint_id,operation_id,generation,state,
                     execution_seq,terminal_seq,terminal_intent,created_at,quiescence_ref)
                    VALUES (?,?,?,?,?,?,'pending',?,?,?,?,?)''',
                           (run['id'], instance_id, operation_id, str(uuid4()), operation_id,
                            binding['store_generation'], execution, terminal[0], intent,
                            persistence.now(), proof_ref))
            elif (prior['quiescence_ref'] is None and prior['state'] == 'pending'
                  and not prior['process_verified'] and prior['barrier_id'] == operation_id):
                db.execute('UPDATE native_checkpoints SET quiescence_ref=? WHERE turn_id=?',
                           (proof_ref, run['id']))
            elif prior['quiescence_ref'] != proof_ref:
                native.fail('checkpoint_scope_mismatch')
        if prior and prior['state'] == 'ready':
            descriptor = json.loads(prior['descriptor'])
            content.resolve(store, descriptor['content'])
            return descriptor
        proof_scope = {**{key: binding[key] for key in state.IDENTITY_KEYS},
                       'instance_id': instance_id, 'turn_id': run['id'], 'barrier_id': operation_id}
        try:
            for pid, identity in ((run['worker_pid'], run['worker_identity']),
                                  (run['child_pid'], run['child_identity'])):
                if alive(pid, identity):
                    native.fail('checkpoint_busy')
            if verify_quiescence(proof_scope) is not True:
                native.fail('checkpoint_busy')
            with store.connect() as db:
                if state.identity(db) != binding:
                    native.fail('checkpoint_scope_mismatch')
                db.execute('UPDATE native_checkpoints SET process_verified=1 WHERE turn_id=?',
                           (run['id'],))
        except Exception as error:
            with store.connect() as db:
                db.execute('UPDATE native_checkpoints SET error_code=? WHERE turn_id=?',
                           (persistence.safe_error(getattr(error, 'code', None))['code'], run['id']))
            persistence.finalize_retry(store, run['id'])
            native.fail('checkpoint_busy')
    # The persistent barrier still fences all execution after releasing the file lock.
    from .service import Checkpoints

    return Checkpoints(store).create(format_version='1', operation_id=operation_id,
                                     params={**{key: binding[key] for key in state.IDENTITY_KEYS},
                                             'instance_id': instance_id, 'turn_id': run['id']})
