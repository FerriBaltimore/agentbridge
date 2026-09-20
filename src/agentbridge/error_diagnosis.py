"""Explicit, bounded AI diagnosis. Submitted work is never silently repeated."""
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from .credentials import environment, CURSOR_KEY_ENV
from .errors import BridgeError
from .models import identifier
from .provider_errors import CANONICAL
from .security import base_environment
from .store import dumps

FAILURE_REASONS = {'invalid_request', 'credential_unavailable', 'sdk_unavailable',
                   'invalid_response', 'response_too_large', 'unexpected_tool', 'provider_failed'}
FAILURE_PHASES = {'setup', 'create', 'send', 'stream', 'wait', 'decode'}


def safe_failure(value):
    if not isinstance(value, dict) or value.get('status') != 'failed':
        return None
    result = {'code': 'diagnosis_failed', 'retryable': False}
    if value.get('reason') in FAILURE_REASONS:
        result['reason'] = value['reason']
    if value.get('phase') in FAILURE_PHASES:
        result['phase'] = value['phase']
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
        if row and row['state'] == 'submitted':
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
    identifier(idempotency_key)
    identifier(model)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 1 <= timeout <= 120):
        raise BridgeError('invalid_timeout', 'Diagnosis timeout must be 1-120 seconds.')
    learning = bridge.error_learning
    case = learning.get_case(case_id)
    account = bridge.resolve_account(account_ref)
    if account.engine != 'cursor':
        raise BridgeError('unsupported', 'Tool-free diagnosis currently requires an explicit Cursor account.')
    payload = dumps({'case_id': case_id, 'account_id': account.id, 'model': model,
                     'timeout': timeout,
                     'binding': hashlib.sha256(dumps(account.to_dict()).encode()).hexdigest()})
    store = bridge.store
    _initialize(store)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT payload FROM error_diagnoses WHERE request_key=?', (idempotency_key,)).fetchone()
        if old:
            if old['payload'] != payload:
                raise BridgeError('idempotency_conflict', 'Diagnostic key belongs to another request.')
        else:
            now, operation_id = time.time(), uuid4().hex
            db.execute('INSERT INTO error_diagnoses VALUES(?,?,?,?,?,?,?)',
                       (idempotency_key, operation_id, payload, 'submitted', None, now, now))
    if old:
        return get(store, idempotency_key)
    try:
        # The diagnostic sees safe vocabulary and structural hints, never the original error.
        evidence = case['evidence']
        request = {'engine': 'cursor', 'model': model, 'evidence': evidence,
                   'targets': sorted(CANONICAL), 'key_env': account.key_env or CURSOR_KEY_ENV}
        result = execute(request, environment(account), timeout)
        failure = safe_failure(result)
        if failure is not None:
            state, result = 'failed', failure
        else:
            proposal = learning.propose(case_id, result, provenance={
                'source': 'ai_session', 'operation_id': operation_id})
            state, result = 'completed', {'proposal_id': proposal['id']}
    except BridgeError as error:
        allowed = CANONICAL | {'diagnosis_timeout', 'diagnosis_invalid', 'credential_unavailable'}
        state, result = 'failed', {'code': error.code if error.code in allowed else 'diagnosis_failed',
                                  'retryable': False}
    except Exception:
        # No exception string, AI output or provider body is stored, including on failure.
        state, result = 'failed', {'code': 'diagnosis_failed', 'retryable': False}
    with store.connect() as db:
        db.execute('UPDATE error_diagnoses SET state=?,result=?,updated=? WHERE request_key=?',
                   (state, dumps(result), time.time(), idempotency_key))
    return get(store, idempotency_key)


def execute(payload, credentials, timeout):
    """Use volatile storage for native SDK state, then remove it after every exit."""
    with tempfile.TemporaryDirectory(prefix='agentbridge-diagnosis-', dir=memory_root()) as folder:
        env = base_environment()
        env.update(credentials)
        env['PYTHONPATH'] = str(Path(__file__).parent.parent)
        for name in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'TMPDIR'):
            env[name] = folder
        child = subprocess.Popen([sys.executable, '-m', 'agentbridge.error_diagnosis_worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, cwd=folder, start_new_session=True)
        selector = selectors.DefaultSelector()
        output = bytearray()
        try:
            child.stdin.write(dumps(payload).encode())
            child.stdin.close()
            os.set_blocking(child.stdout.fileno(), False)
            selector.register(child.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise BridgeError('diagnosis_timeout', 'The diagnostic time limit was reached.')
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fd, 8193)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    output.extend(chunk)
                    if len(output) > 8192:
                        raise BridgeError('diagnosis_invalid', 'Diagnostic output exceeded its bound.')
            child.wait(timeout=max(0.01, deadline - time.monotonic()))
            if child.returncode:
                raise BridgeError('diagnosis_failed', 'The diagnostic provider did not complete.')
            return json.loads(output)
        finally:
            selector.close()
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)
            child.stdout.close()


def memory_root():
    """Fail closed when a verified volatile filesystem is unavailable."""
    try:
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            mounted, kind = line.split(' - ', 1)
            if mounted.split()[4] == '/dev/shm' and kind.split()[0] == 'tmpfs':
                if os.access('/dev/shm', os.W_OK | os.X_OK):
                    return '/dev/shm'
    except (OSError, ValueError, IndexError):
        pass
    raise BridgeError('unsupported', 'Isolated diagnosis requires a writable verified tmpfs at /dev/shm.')
