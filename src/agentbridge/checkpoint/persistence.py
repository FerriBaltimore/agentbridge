"""Turn/checkpoint association and late observations share the terminal transaction."""

from datetime import datetime, timezone
import json
from uuid import uuid4

from ..errors import BridgeError
from ..models import TERMINAL
from .state import canonical_uuid, cursor, identity


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_error(code):
    mapped = {'checkpoint_unsafe_path': 'checkpoint_corrupt',
              'checkpoint_incompatible': 'runtime_incompatible',
              'checkpoint_scope_mismatch': 'scope_mismatch',
              'checkpoint_too_large': 'checkpoint_incomplete',
              'checkpoint_invalid': 'checkpoint_incomplete',
              'checkpoint_disabled': 'dependency_missing',
              'native_session_missing': 'checkpoint_incomplete',
              'native_version_unverified': 'runtime_incompatible'}.get(code, code)
    if mapped not in {'checkpoint_busy', 'checkpoint_incomplete', 'checkpoint_corrupt',
                      'runtime_incompatible', 'scope_mismatch', 'unsupported_version',
                      'dependency_missing', 'cursor_generation_mismatch', 'cursor_rollback',
                      'checkpoint_sql_backup_required'}:
        mapped = 'checkpoint_incomplete'
    return {'code': mapped, 'retryable': mapped in {'checkpoint_busy', 'checkpoint_incomplete'}}


def record(db, turn_id):
    row = db.execute('SELECT * FROM native_checkpoints WHERE turn_id=?', (turn_id,)).fetchone()
    return dict(row) if row else None


def needed(db, run):
    if db.execute('SELECT 1 FROM evaluation_instances WHERE session_id=?',
                  (run['session_id'],)).fetchone():
        return 'ephemeral_instance'
    launched = db.execute('SELECT may_have_started FROM native_session_launches WHERE run_id=?',
                          (run['id'],)).fetchone()
    evidence = db.execute("SELECT 1 FROM events WHERE run_id=? AND kind IN "
                          "('session','run_started','assistant','text_delta','tool_call')",
                          (run['id'],)).fetchone()
    if not (run['child_pid'] or launched and launched[0] or evidence):
        return 'not_started'
    return None


def prepare_terminal(store, turn_id, state, code, exit_code, *, process_verified=False):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        binding = identity(db)
        if not binding['enabled']:
            return
        run = db.execute('SELECT * FROM runs WHERE id=?', (turn_id,)).fetchone()
        if run is None or run['state'] in TERMINAL or needed(db, run):
            return
        previous = record(db, turn_id)
        if previous is None:
            boundary = db.execute('SELECT COALESCE(MAX(seq),0) FROM events WHERE run_id=?',
                                  (turn_id,)).fetchone()[0]
            if run['stop_requested'] and state != 'interrupted':
                state, code = 'cancelled', 'user_stop'
            intent = json.dumps({'state': state, 'code': code, 'exit_code': exit_code})
            barrier = str(uuid4())
            db.execute('''INSERT INTO native_checkpoints
                (turn_id,instance_id,barrier_id,checkpoint_id,operation_id,generation,state,
                 execution_seq,process_verified,terminal_intent,created_at)
                VALUES (?,?,?,?,?,?,'pending',?,?,?,?)''',
                       (turn_id, run['session_id'], barrier, str(uuid4()), barrier,
                        binding['store_generation'], boundary, int(process_verified), intent, now()))
    if binding['checkpoint_mode'] == 'on_demand':
        return
    # Native copying must not hold the Store writer transaction or any Fullbrain transaction.
    from .service import seal
    try:
        seal(store, turn_id)
    except Exception as error:
        # Historical results survive sealing failures; raw exception/provider bodies do not.
        with store.connect() as db:
            db.execute('UPDATE native_checkpoints SET error_code=? WHERE turn_id=?',
                       (safe_error(getattr(error, 'code', 'checkpoint_incomplete'))['code'], turn_id))


def attachment(db, run):
    if not identity(db)['enabled']:
        return None
    value = record(db, run['id'])
    if value and value['descriptor'] and value['state'] in {'sealed_local', 'ready'}:
        return {'format_version': '1', 'state': 'sealed',
                'checkpoint': json.loads(value['descriptor'])}
    if value:
        return {'format_version': '1', 'state': 'pending', 'barrier_id': value['barrier_id'],
                'error': safe_error(value['error_code'] or 'checkpoint_incomplete')}
    reason = needed(db, run)
    if reason:
        return {'format_version': '1', 'state': 'not_required', 'reason': reason}
    raise BridgeError('checkpoint_incomplete', 'The required admission barrier is missing.')


def observe(store, db, turn_id, terminal_seq=None):
    value = record(db, turn_id)
    if not value:
        return
    if terminal_seq is not None:
        db.execute('UPDATE native_checkpoints SET terminal_seq=? WHERE turn_id=?',
                   (terminal_seq, turn_id))
        value['terminal_seq'] = terminal_seq
    if value['terminal_seq'] is None:
        return
    ready = value['state'] in {'sealed_local', 'ready'}
    kind = 'checkpoint_ready' if ready else 'checkpoint_pending'
    if value['observation_seq'] is not None:
        existing = db.execute('SELECT kind FROM events WHERE seq=?',
                              (value['observation_seq'],)).fetchone()
        if existing and existing['kind'] == kind:
            return
    session = db.execute('SELECT native_id FROM sessions WHERE id=?',
                         (value['instance_id'],)).fetchone()
    try:
        native_id = canonical_uuid(session['native_id'])
    except BridgeError:
        native_id = None
    sequence = store._event(db, turn_id, value['instance_id'], kind, {})
    payload = {
        'format_version': '1', 'event_id': str(uuid4()), 'kind': kind.replace('_', '.'),
        'at': now(), 'cursor': cursor(db, sequence), 'instance_id': value['instance_id'],
        'turn_id': turn_id, 'native_id': native_id, 'terminal_seq': value['terminal_seq'],
        'barrier_id': value['barrier_id'], 'final': False,
    }
    if ready:
        payload['checkpoint'] = json.loads(value['descriptor'])
    else:
        payload['error'] = safe_error(value['error_code'] or 'checkpoint_incomplete')
    db.execute('UPDATE events SET data=? WHERE seq=?', (json.dumps(payload), sequence))
    db.execute('UPDATE native_checkpoints SET observation_seq=?,state=? WHERE turn_id=?',
               (sequence, 'ready' if ready else 'pending', turn_id))


def finalize_retry(store, turn_id):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        observe(store, db, turn_id)
