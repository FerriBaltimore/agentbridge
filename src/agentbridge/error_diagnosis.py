"""Read historical AI diagnosis receipts while proxy diagnosis is unavailable."""
import json
import time

from .errors import BridgeError
from .models import identifier
from .provider_errors import CANONICAL
from .store import dumps

FAILURE_REASONS = {'invalid_request', 'credential_unavailable', 'sdk_unavailable',
                   'invalid_response', 'response_too_large', 'unexpected_tool', 'provider_failed'}
FAILURE_PHASES = {'setup', 'create', 'send', 'stream', 'wait', 'decode'}


def safe_failure(value):
    """Return the bounded public result for a historical diagnostic failure."""
    if not isinstance(value, dict) or value.get('status') != 'failed':
        return None
    result = {'code': 'diagnosis_failed', 'retryable': False}
    reason = value.get('reason')
    phase = value.get('phase')
    if isinstance(reason, str) and reason in FAILURE_REASONS:
        result['reason'] = reason
    if isinstance(phase, str) and phase in FAILURE_PHASES:
        result['phase'] = phase
    code = value.get('provider_code')
    if isinstance(code, str) and code in CANONICAL:
        result['provider_code'] = code
    return result


def _initialize(store):
    with store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS error_diagnoses(
            request_key TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL,
            payload TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
            created REAL NOT NULL, updated REAL NOT NULL)''')


def get(store, idempotency_key):
    identifier(idempotency_key)
    _initialize(store)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM error_diagnoses WHERE request_key=?', (idempotency_key,)).fetchone()
        proposals_exist = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='error_proposals'"
        ).fetchone()
        if row and row['state'] == 'submitted' and proposals_exist:
            proposal = db.execute('''SELECT id FROM error_proposals
                WHERE json_extract(provenance,'$.operation_id')=? AND
                      json_extract(provenance,'$.source')='ai_session'
                ORDER BY created_at LIMIT 1''', (row['operation_id'],)).fetchone()
            if proposal:
                db.execute("UPDATE error_diagnoses SET state='completed',result=?,updated=? WHERE request_key=?",
                    (dumps({'proposal_id': proposal['id']}), time.time(), idempotency_key))
                row = db.execute('SELECT * FROM error_diagnoses WHERE request_key=?', (idempotency_key,)).fetchone()
    if not row:
        raise BridgeError('not_found', 'The diagnostic operation does not exist.')
    return {'operation_id': row['operation_id'], 'state': row['state'],
            'result': json.loads(row['result']) if row['result'] else None,
            'created_at': row['created'], 'updated_at': row['updated'],
            'retryable': False, 'may_be_running': row['state'] == 'submitted'}


def diagnose(bridge, case_id, *, account_ref, model, idempotency_key, timeout=60):
    raise BridgeError('unsupported_operation',
                      'AI error diagnosis requires a verified proxy implementation.')
