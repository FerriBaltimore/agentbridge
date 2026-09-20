"""Atomically bind a verified provider identity and its non-secret references."""
import json
import time

from .account_probe import safe_identity
from .errors import BridgeError, BusyError
from .models import Account, account_name_key
from .auth_store import dumps


def bind_account(store, attempt, activation, connection):
    if (not isinstance(activation, dict) or activation.get('provider') != attempt['engine']
            or activation.get('attempt_id') != attempt['grantbridge_id']):
        raise BridgeError('activation_invalid', 'The activated provider does not match the attempt.')
    identity = safe_identity(activation.get('identity'))
    email = identity.get('email')
    if not email:
        raise BridgeError('identity_missing', 'The provider did not verify an account identity.')
    if attempt.get('email') and email != attempt['email']:
        raise BridgeError('identity_changed', 'The provider identity does not match the selected account.')
    home = activation.get('home')
    reference = None
    if attempt['engine'] == 'cursor':
        remote = activation.get('credential_ref')
        if (not connection or not isinstance(remote, dict) or remote.get('provider') != 'cursor'
                or remote.get('attempt_id') != attempt['grantbridge_id']):
            raise BridgeError('activation_invalid', 'Cursor requires a verified managed credential reference.')
        reference = {'attempt_id': remote['attempt_id'], 'owner_ref': attempt['owner'],
                     'connection': connection}
    elif not isinstance(home, str) or not home:
        raise BridgeError('activation_unsupported', 'The provider did not expose an executable account home.')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = dict(db.execute('SELECT * FROM auth_attempts WHERE id=?', (attempt['id'],)).fetchone())
        if current['status'] == 'bound':
            saved = db.execute('SELECT config FROM accounts WHERE id=?', (current['account_id'],)).fetchone()
            current['data'] = json.loads(current['data'])
            return Account(**json.loads(saved['config'])), current
        if current['status'] != 'verified':
            raise BridgeError('authentication_not_verified', 'The attempt is no longer verified.')
        active = db.execute("SELECT 1 FROM runs WHERE account_id=? AND state IN ('starting','running','stopping')",
                            (attempt['account_id'],)).fetchone()
        if active:
            raise BusyError()
        previous = db.execute('SELECT config FROM accounts WHERE id=?', (attempt['account_id'],)).fetchone()
        previous = json.loads(previous['config']) if previous else {}
        if previous.get('email') and previous['email'] != email:
            raise BridgeError('identity_changed', 'Reauthentication cannot replace the account identity.')
        account = Account(attempt['account_id'], attempt['engine'], home=home,
                          name=attempt['name'], email=email or attempt['email'],
                          env_names=previous.get('env_names', ()),
                          key_env=None if reference else previous.get('key_env'),
                          command=previous.get('command', ()), credential_ref=reference)
        for record in db.execute('SELECT id,config FROM accounts WHERE id<>?', (account.id,)):
            other = json.loads(record['config'])
            if other.get('name') and account_name_key(other['name']) == account_name_key(account.name):
                raise BridgeError('account_name_in_use', 'Account names must be unique.')
            if home and other.get('home') == account.home and other['engine'] == account.engine:
                raise BridgeError('home_in_use', 'This native home already has an account ID.')
        now = time.time()
        data = {**json.loads(current['data']), 'identity': identity, 'state': 'usable'}
        db.execute('INSERT INTO accounts VALUES (?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config',
                   (account.id, dumps(account.to_dict())))
        db.execute('''INSERT INTO account_observations(account_id,observed_at,source,status,data)
            VALUES (?,?,?,?,?)''', (account.id, now, 'grantbridge', 'usable',
                                    dumps({'identity': identity, 'state': 'usable',
                                           'source': 'grantbridge_native_check'})))
        db.execute("UPDATE auth_attempts SET status='bound',data=?,updated=? WHERE id=?",
                   (dumps(data), now, attempt['id']))
    return account, store.get_auth_attempt(attempt['id'], attempt['owner'])
