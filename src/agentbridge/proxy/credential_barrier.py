"""Persistent account-scoped holds shared by admission, login and the proxy supervisor."""

from hashlib import sha256
import json

from ..errors import BridgeError
from ..models import identifier


def account_lock(store, account_id):
    from ..checkpoint import content

    identifier(account_id)
    return content.instance_lock(store, 'credential-' + sha256(account_id.encode()).hexdigest())


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS credential_capture_operations(
        operation_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL,
        proof_ref TEXT, descriptor TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS credential_account_holds(
        account_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL, reason TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS credential_restored_accounts(
        account_id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, descriptor TEXT NOT NULL,
        status TEXT NOT NULL, proof_ref TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS credential_reconnections(
        proof_ref TEXT PRIMARY KEY, account_id TEXT NOT NULL, generation TEXT NOT NULL,
        status TEXT NOT NULL, authorized_at REAL NOT NULL)''')


def hold(db, account_id):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='credential_account_holds'").fetchone()
    return db.execute('SELECT operation_id,reason FROM credential_account_holds WHERE account_id=?',
                       (account_id,)).fetchone() if exists else None


def require_account(db, account_id, *, login=False):
    current = hold(db, account_id)
    if current and not (login and current[1] == 'reconnect'):
        raise BridgeError('credential_snapshot_pending',
                          'This account is held for credential capture or recovery.', retryable=True)


def supervisor_hold(root, account_id):
    from ..storage.inspection import connect_selected

    with connect_selected(root) as db:
        return hold(db, account_id) if db is not None else None


def require_supervisor(root, account_id):
    if supervisor_hold(root, account_id):
        raise BridgeError('credential_snapshot_pending',
                          'This managed proxy is held for credential capture or recovery.')


def require_drained(db, account_ids):
    for account_id in account_ids:
        active = db.execute("SELECT 1 FROM runs WHERE account_id=? "
                             "AND state IN ('starting','running','stopping')", (account_id,)).fetchone()
        login = db.execute("SELECT 1 FROM auth_attempts WHERE account_id=? AND status NOT IN "
                            "('failed','cancelled','expired','interrupted','abandoned','revoked',"
                            "'replaced','bound','usable')", (account_id,)).fetchone()
        if active or login:
            raise BridgeError('credential_snapshot_pending',
                              'Drain account execution and authentication before credential capture.',
                              retryable=True)


def reset_restored_authority(db, generation):
    """Imported proofs never authorize the new generation, even on a second restoration."""
    from .managed import ManagedProxyClient

    initialize(db)
    db.execute('DELETE FROM credential_restored_accounts')
    db.execute('DELETE FROM credential_reconnections')
    pending = db.execute("SELECT id,status,data FROM auth_attempts WHERE status NOT IN "
                         "('bound','usable','failed','cancelled','expired','interrupted',"
                         "'abandoned','revoked','replaced')").fetchall()
    for row in pending:
        data = json.loads(row['data'])
        data['recovery'] = {'previous_status': row['status'], 'outcome': 'unknown',
                            'destination_generation': generation}
        data['error'] = {'code': 'authentication_outcome_unknown'}
        db.execute("UPDATE auth_attempts SET status='interrupted',data=? WHERE id=?",
                   (json.dumps(data), row['id']))
    for row in db.execute('SELECT id,config FROM accounts'):
        if ManagedProxyClient.is_managed(json.loads(row['config']), row['id']):
            db.execute('INSERT INTO credential_account_holds VALUES (?,?,?) '
                       'ON CONFLICT(account_id) DO UPDATE SET '
                       'operation_id=excluded.operation_id,reason=excluded.reason',
                       (row['id'], generation, 'restore_pending'))
