"""Atomically activate a GrantBridge login as one routed proxy account."""

import json
import re
import time

from .auth_store import dumps
from .errors import BridgeError, BusyError
from .models import Account, account_name_key


_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


def _same_email(left, right):
    return (isinstance(left, str) and isinstance(right, str)
            and left.casefold() == right.casefold())


def bind_proxy_account(store, attempt, route_config, observation):
    """Bind only a verified, single-credential proxy to the authenticated name."""
    if attempt['status'] != 'verified':
        raise BridgeError('authentication_not_verified', 'Verify the proxy account before activation.')
    if (observation.get('provider') != attempt['engine']
            or observation.get('status') != 'active'
            or observation.get('disabled') is not False
            or observation.get('unavailable') is not False):
        raise BridgeError('proxy_binding_unverified', 'The local proxy account is not usable.')
    binding = observation.get('binding_fingerprint')
    identity = observation.get('identity_fingerprint')
    if not all(isinstance(value, str) and _FINGERPRINT.fullmatch(value)
               for value in (binding, identity)):
        raise BridgeError('proxy_binding_unverified', 'The proxy credential identity is unavailable.')
    email = observation.get('email')
    if attempt.get('email') and not _same_email(attempt['email'], email):
        raise BridgeError('identity_changed', 'The proxy identity does not match the selected account.')
    models = tuple(item['id'] for item in observation.get('models', ())
                   if isinstance(item, dict) and isinstance(item.get('id'), str))
    if not models:
        raise BridgeError('model_unavailable', 'The local proxy exposes no usable models.')
    account = Account(
        id=attempt['account_id'], engine='codex', name=attempt['name'],
        email=email, provider=attempt['engine'], supported_models=models,
        proxy_base_url=route_config['proxy_base_url'], key_env=route_config['key_env'],
        management_key_env=route_config['management_key_env'],
    )
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT status,data FROM auth_attempts WHERE id=? AND owner=?',
                             (attempt['id'], attempt['owner'])).fetchone()
        if current is None or current['status'] != 'verified':
            raise BridgeError('authentication_not_verified', 'The login is no longer verified.')
        if db.execute("SELECT 1 FROM account_reset_attempts WHERE account_id=? "
                      "AND state='pending'", (account.id,)).fetchone():
            raise BridgeError('reset_pending',
                              'Resolve the pending reset attempt before activating this login.')
        previous_row = db.execute('SELECT config FROM accounts WHERE id=?', (account.id,)).fetchone()
        previous = Account(**json.loads(previous_row['config'])) if previous_row else None
        if previous is not None:
            if (previous.engine != 'codex' or not previous.proxy_base_url
                    or previous.name != account.name or previous.provider != account.provider
                    or previous.proxy_base_url != account.proxy_base_url
                    or previous.key_env != account.key_env
                    or previous.management_key_env != account.management_key_env):
                raise BridgeError('account_changed', 'Reauthentication cannot change the account route.')
            if previous.email and email and not _same_email(previous.email, email):
                raise BridgeError('identity_changed', 'Reauthentication cannot replace the account identity.')
        active = db.execute("SELECT 1 FROM runs WHERE account_id=? AND state IN ('starting','running','stopping')",
                            (account.id,)).fetchone()
        if active:
            raise BusyError()
        for row in db.execute('''SELECT id,config FROM accounts WHERE id<>?
            AND id NOT IN (SELECT account_id FROM retired_accounts)''', (account.id,)):
            other = json.loads(row['config'])
            if ((other.get('provider') or other.get('engine')) == account.provider
                    and other.get('name')
                    and account_name_key(other['name']) == account_name_key(account.name)):
                raise BridgeError('account_name_in_use',
                                  'Account names must be unique within each provider.')
            if other.get('proxy_base_url') == account.proxy_base_url:
                raise BridgeError('proxy_endpoint_shared', 'The proxy endpoint belongs to another account.')
        old_binding = db.execute('SELECT identity_fingerprint FROM proxy_bindings WHERE account_id=?',
                                 (account.id,)).fetchone()
        if old_binding and old_binding['identity_fingerprint'] != identity:
            raise BridgeError('identity_changed', 'Reauthentication cannot replace the account identity.')
        other_binding = db.execute('SELECT account_id FROM proxy_bindings WHERE identity_fingerprint=?',
                                   (identity,)).fetchone()
        if other_binding and other_binding['account_id'] != account.id:
            raise BridgeError('proxy_binding_shared', 'The proxy identity belongs to another account.')
        now = time.time()
        db.execute('INSERT INTO accounts(id,config) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config',
                   (account.id, dumps(account.to_dict())))
        db.execute('INSERT INTO proxy_bindings(account_id,binding_fingerprint,identity_fingerprint) '
                   'VALUES (?,?,?) ON CONFLICT(account_id) DO UPDATE SET '
                   'binding_fingerprint=excluded.binding_fingerprint',
                   (account.id, binding, identity))
        remote = json.loads(current['data'])
        remote['identity'] = {'email': email} if email else {}
        remote['state'] = 'usable'
        db.execute("UPDATE auth_attempts SET status='bound',data=?,updated=? WHERE id=?",
                   (dumps(remote), now, attempt['id']))
        db.execute('INSERT INTO account_observations(account_id,observed_at,source,status,data) '
                   'VALUES (?,?,?,?,?)', (account.id, now, 'grantbridge_proxy', 'usable',
                   dumps({'identity': remote['identity'], 'state': 'usable',
                          'source': 'grantbridge_proxy_check'})))
    return account, store.get_auth_attempt(attempt['id'], attempt['owner'])
