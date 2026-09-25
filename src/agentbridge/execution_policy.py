"""Persist conversation execution defaults separately from upstream routing."""

import json

from .errors import BridgeError


DEFAULT_POLICY = {'permission_mode': 'dontAsk', 'sandbox_mode': 'read-only'}


def validate_policy(permission_mode, sandbox_mode):
    if permission_mode not in ('dontAsk', 'default'):
        raise BridgeError('invalid_permissions', 'Choose dontAsk or default permission mode.')
    if sandbox_mode not in ('read-only', 'workspace-write', 'danger-full-access'):
        raise BridgeError('invalid_sandbox', 'Choose a supported Codex sandbox mode.')
    return {'permission_mode': permission_mode, 'sandbox_mode': sandbox_mode}


def read_policy(db, session_id):
    row = db.execute('SELECT permission_mode,sandbox_mode FROM instance_execution_policies '
                     'WHERE session_id=?', (session_id,)).fetchone()
    return dict(row) if row else dict(DEFAULT_POLICY)


def write_policy(db, session_id, policy):
    values = validate_policy(**policy)
    db.execute('INSERT INTO instance_execution_policies(session_id,permission_mode,sandbox_mode) '
               'VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET '
               'permission_mode=excluded.permission_mode,sandbox_mode=excluded.sandbox_mode',
               (session_id, values['permission_mode'], values['sandbox_mode']))


def policy_payload(policy):
    """Keep historical default creation keys stable while binding custom policy."""
    return {key: value for key, value in policy.items() if value != DEFAULT_POLICY[key]}


def previous_message_policy(store, request_key, instance_id):
    """Recover frozen omitted defaults before the existing exact replay checks."""
    if not request_key:
        return None
    with store.connect() as db:
        row = db.execute('SELECT options FROM runs WHERE request_key=? AND session_id=?',
                         (request_key, instance_id)).fetchone()
    if row is None:
        return None
    options = json.loads(row['options'])
    return {'permission_mode': options.get('permission_mode', 'dontAsk'),
            'sandbox_mode': options.get('sandbox', 'read-only')}


def migrate_v13(db, version):
    if version != 12:
        return version
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    current = db.execute('SELECT version FROM metadata').fetchone()[0]
    if current != 12:
        return current
    db.execute('''CREATE TABLE IF NOT EXISTS instance_execution_policies(
            session_id TEXT PRIMARY KEY,
            permission_mode TEXT NOT NULL,
            sandbox_mode TEXT NOT NULL)''')
    db.execute("INSERT OR IGNORE INTO instance_execution_policies "
               "SELECT id,'dontAsk','read-only' FROM sessions")
    db.execute('UPDATE metadata SET version=13')
    return 13
