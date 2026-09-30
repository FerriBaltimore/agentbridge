"""Normalize copied in-flight execution to explicit, held unknown outcomes without replay."""

import json
from uuid import uuid4

from . import persistence, state


def interrupt_copied_execution(db):
    from ..store import Store
    from ..queueing.records import finished
    from ..turn_outcome import completion

    binding = state.identity(db)
    rows = db.execute("SELECT * FROM runs WHERE state IN ('starting','running','stopping')").fetchall()
    for run in rows:
        previous = persistence.record(db, run['id'])
        if previous is None:
            barrier = str(uuid4())
            execution = db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE run_id=?',
                                   (run['id'],)).fetchone()[0]
            intent = json.dumps({'state': 'interrupted', 'code': 'restore_unknown_outcome',
                                 'exit_code': None})
            db.execute('''INSERT INTO native_checkpoints
                (turn_id,instance_id,barrier_id,checkpoint_id,operation_id,generation,state,
                 execution_seq,terminal_intent,created_at,error_code)
                VALUES (?,?,?,?,?,?,'pending',?,?,?,'checkpoint_incomplete')''',
                       (run['id'], run['session_id'], barrier, str(uuid4()), barrier,
                        binding['store_generation'], execution, intent, persistence.now()))
        else:
            db.execute("UPDATE native_checkpoints SET state='pending',process_verified=0,"
                       "error_code='checkpoint_incomplete' WHERE turn_id=?", (run['id'],))
        Store._event(db, run['id'], run['session_id'], 'recovery', {
            'reason': 'restored_inflight_execution', 'automatic_retry': False, 'outcome': 'unknown'})
        result = completion(db, run['id'], 'interrupted', 'restore_unknown_outcome', None)
        result['durability'] = persistence.attachment(db, run)
        terminal = Store._event(db, run['id'], run['session_id'], 'run_finished', result)
        db.execute("UPDATE runs SET state='interrupted',error='restore_unknown_outcome',"
                   "stop_requested=1 WHERE id=?", (run['id'],))
        finished(Store, db, run, 'interrupted')
        persistence.observe(Store, db, run['id'], terminal)


def hold_import(db, destination, snapshot, generation, workspace_paths):
    """Invalidate copied process/credential authority and retain the source replay floor."""
    from .workspaces import rebind
    from ..proxy.credential_barrier import reset_restored_authority

    db.execute('UPDATE store_identity SET store_generation=?,source_generation=?,source_seq=?, '
               'recovery_held=1,enabled=1 WHERE singleton=1',
               (generation, snapshot['store_generation'], snapshot['cursor']['seq']))
    db.execute('UPDATE runs SET worker_pid=NULL,worker_identity=NULL,child_pid=NULL,'
               'child_identity=NULL')
    db.execute('UPDATE conversation_queues SET dispatcher_pid=NULL,dispatcher_identity=NULL')
    db.execute('DELETE FROM recovery_workspaces')
    rebind(db, destination, workspace_paths)
    interrupt_copied_execution(db)
    reset_restored_authority(db, generation)
