"""Persistent, non-blocking authentication orchestration through GrantBridge."""
import time
import hashlib
from uuid import uuid4

from .errors import BridgeError
from .grantbridge import GrantBridgeClient
from .models import ENGINES, account_name_key, identifier
from .auth_runtime import AuthRuntime
from .auth_binding import bind_account


TERMINAL = {'failed', 'cancelled', 'expired', 'interrupted', 'revoked', 'replaced'}


class AuthenticationService:
    def __init__(self, store, accounts):
        self.store = store
        self.accounts = accounts
        self.runtime = AuthRuntime(store)

    def start(self, *, engine, name, email=None, grantbridge_root=None, data_dir=None,
              mode='browser', browser='same_host', request_key=None, owner_ref=None, client=None):
        if engine not in ENGINES:
            raise BridgeError('invalid_engine', 'Unknown engine.')
        if not isinstance(name, str) or not name.strip():
            raise BridgeError('invalid_name', 'Account name is required.')
        existing = next((item for item in self.accounts.list()
                         if item.name and account_name_key(item.name) == account_name_key(name)), None)
        if existing and existing.engine != engine:
            raise BridgeError('account_engine_mismatch', 'The existing account name belongs to a different engine.')
        owner = owner_ref or self._owner_for(request_key)
        identifier(owner)
        if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 256):
            raise BridgeError('invalid_request', 'request_key must contain 1-256 characters.')
        connection = None if client is not None else GrantBridgeClient(
            grantbridge_root, data_dir=data_dir).configuration()
        attempt = {
            'id': uuid4().hex,
            'owner': owner,
            'account_id': existing.id if existing else uuid4().hex,
            'engine': engine,
            'name': existing.name if existing else name.strip(),
            'email': email,
            'mode': mode,
            'browser': browser,
            'status': 'starting',
            'data': {},
        }
        stored, created = self.store.create_auth_attempt(attempt, request_key=request_key, connection=connection)
        if not created:
            return self._public(stored)
        if client is None:
            self.runtime.launch(attempt['id'], 'start')
            return self._public(stored)
        try:
            remote = client.start(owner=attempt['owner'], engine=engine, mode=mode,
                                  browser=browser, request_key=attempt['id'])
            stored = self.store.update_auth_attempt(
                attempt['id'], attempt['owner'], grantbridge_id=remote.get('id'),
                status=self._status(remote), data=remote)
        except BridgeError as error:
            stored = self.store.update_auth_attempt(
                attempt['id'], attempt['owner'], status='failed',
                data={'error': {'code': error.code}})
            raise
        return self._public(stored)

    def status(self, attempt_id, *, owner_ref=None, account_ref=None,
               grantbridge_root=None, data_dir=None, client=None):
        row = self._owned(attempt_id, owner_ref, account_ref)
        self.runtime.reap()
        job = self.runtime.get(attempt_id)
        if client is None and self.runtime.active(job):
            return {**self._public(row), 'checking': job['kind'] != 'start',
                    'cancel_requested': bool(job['cancel_requested'])}
        if row['status'] in TERMINAL | {'bound', 'usable'}:
            return self._public(row)
        if not row['grantbridge_id']:
            if job and not job['job_id'] and time.time() - row['created'] < 15:
                return self._public(row)
            if job and not self.runtime.active(job):
                with self._client(row) as probe:
                    remote = probe.find(attempt_id, row['owner'])
                if remote:
                    row = self.store.update_auth_attempt(attempt_id, row['owner'],
                        grantbridge_id=remote['id'], status=self._status(remote), data=remote)
                    return self._public(row)
                row = self.store.update_auth_attempt(attempt_id, row['owner'], status='interrupted',
                    data={'error': {'code': 'authentication_interrupted'}})
            return self._public(row)
        owns_client = client is None
        client = client or self._client(row, grantbridge_root, data_dir)
        try:
            remote = client.get(row['grantbridge_id'], row['owner'])
            row = self._save_remote(row, remote)
        finally:
            if owns_client:
                client.close()
        return self._public(row)

    def check(self, attempt_id, *, owner_ref=None, account_ref=None,
              grantbridge_root=None, data_dir=None, client=None, inference=False):
        if not isinstance(inference, bool):
            raise BridgeError('invalid_params', 'inference must be a boolean.')
        row = self._owned(attempt_id, owner_ref, account_ref)
        if row['status'] in TERMINAL:
            raise BridgeError('authentication_required', 'This authentication attempt has ended.')
        if row['status'] == 'bound':
            return self._public(row)
        if not row['grantbridge_id']:
            raise BridgeError('authentication_attempt_not_ready', 'Authentication attempt has not started.')
        if client is None and self.runtime.get(attempt_id):
            if row['status'] not in {'authorized', 'verified'}:
                raise BridgeError('authentication_attempt_not_ready', 'Authorize the login before checking it.')
            self.runtime.launch(attempt_id, 'inference' if inference else 'check')
            return {**self._public(row), 'checking': True}
        owns_client = client is None
        client = client or self._client(row, grantbridge_root, data_dir, timeout=160)
        try:
            remote = (client.check(row['grantbridge_id'], row['owner'], inference=True) if inference else
                      client.check(row['grantbridge_id'], row['owner']))
            row = self._save_remote(row, remote)
        finally:
            if owns_client:
                client.close()
        return self._public(row)

    def cancel(self, attempt_id, *, owner_ref=None, account_ref=None,
               grantbridge_root=None, data_dir=None, client=None):
        row = self._owned(attempt_id, owner_ref, account_ref)
        if row['status'] in TERMINAL or row['status'] in {'bound', 'usable'}:
            return self._public(row)
        if self.runtime.get(attempt_id):
            self.runtime.request_cancel(attempt_id)
        if row['status'] in {'authorized', 'verified'}:
            row = self.store.update_auth_attempt(attempt_id, row['owner'], status='cancelled')
            return self._public(row)
        if not row['grantbridge_id']:
            if not self.runtime.active(self.runtime.get(attempt_id)):
                row = self.store.update_auth_attempt(attempt_id, row['owner'], status='cancelled')
            return {**self._public(row), 'cancel_requested': True}
        owns_client = client is None
        client = client or self._client(row, grantbridge_root, data_dir)
        try:
            remote = client.cancel(row['grantbridge_id'], row['owner'])
            row = self._save_remote(row, remote)
        finally:
            if owns_client:
                client.close()
        return self._public(row)

    def complete(self, attempt_id, *, owner_ref=None, account_ref=None,
                 grantbridge_root=None, data_dir=None, client=None):
        row = self._owned(attempt_id, owner_ref, account_ref)
        if row['status'] == 'bound':
            account = self.accounts.get(row['account_id'])
            return {'account': account.to_dict(), 'attempt': self._public(row),
                    'identity': row['data'].get('identity', {}), 'home': account.home}
        if row['status'] != 'verified':
            raise BridgeError('authentication_not_verified', 'A fresh provider check must pass before activation.')
        job = self.runtime.get(attempt_id)
        if self.runtime.active(job) and job['kind'] != 'start':
            raise BridgeError('authentication_attempt_not_ready', 'Provider verification is still running.')
        owns_client = client is None
        client = client or self._client(row, grantbridge_root, data_dir)
        try:
            activation = client.activate(row['grantbridge_id'], row['owner'])
        finally:
            if owns_client:
                client.close()
        job = self.runtime.get(attempt_id)
        account, row = bind_account(self.store, row, activation, job['config'] if job else None)
        return {'account': account.to_dict(), 'attempt': self._public(row),
                'identity': row['data'].get('identity', {}), 'home': account.home}

    def login(self, *, engine, name, email=None, grantbridge_root=None, data_dir=None,
              mode='browser', browser='same_host', timeout=600, poll_interval=1.0,
              on_attempt=None, client=None, inference=False):
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 86400:
            raise BridgeError('invalid_timeout', 'Login timeout must be in [1, 86400] seconds.')
        if not isinstance(poll_interval, (int, float)) or not 0.05 <= poll_interval <= 60:
            raise BridgeError('invalid_poll_interval', 'Login poll interval must be in [0.05, 60] seconds.')
        try:
            attempt = self.start(engine=engine, name=name, email=email,
                                 grantbridge_root=grantbridge_root, data_dir=data_dir,
                                 mode=mode, browser=browser, client=client)
            if on_attempt:
                on_attempt(attempt)
            started = time.monotonic()
            while attempt['status'] not in TERMINAL | {'authorized', 'verified', 'bound', 'usable'}:
                if time.monotonic() - started >= timeout:
                    self.cancel(attempt['attempt_id'], owner_ref=attempt['owner_ref'],
                                grantbridge_root=grantbridge_root, data_dir=data_dir, client=client)
                    raise BridgeError('login_timeout', 'The authentication attempt timed out.')
                time.sleep(poll_interval)
                attempt = self.status(attempt['attempt_id'], owner_ref=attempt['owner_ref'],
                                      grantbridge_root=grantbridge_root, data_dir=data_dir, client=client)
                if on_attempt:
                    on_attempt(attempt)
            if attempt['status'] in TERMINAL:
                raise BridgeError('login_failed', 'The provider did not complete authentication.')
            while client is None and self.runtime.active(self.runtime.get(attempt['attempt_id'])):
                if time.monotonic() - started >= timeout:
                    raise BridgeError('login_timeout', 'Authentication is still running.')
                time.sleep(poll_interval)
            attempt = self.check(attempt['attempt_id'], owner_ref=attempt['owner_ref'],
                                 grantbridge_root=grantbridge_root, data_dir=data_dir, client=client, inference=inference)
            while client is None and self.runtime.active(self.runtime.get(attempt['attempt_id'])):
                if time.monotonic() - started >= timeout:
                    raise BridgeError('login_timeout', 'The provider verification is still running.')
                time.sleep(poll_interval)
                attempt = self.status(attempt['attempt_id'], owner_ref=attempt['owner_ref'])
            if client is None:
                attempt = self.status(attempt['attempt_id'], owner_ref=attempt['owner_ref'])
            if on_attempt:
                on_attempt(attempt)
            return self.complete(attempt['attempt_id'], owner_ref=attempt['owner_ref'],
                                 grantbridge_root=grantbridge_root, data_dir=data_dir, client=client)
        finally:
            if client is not None:
                client.close()

    def _client(self, row, root=None, data_dir=None, **options):
        job = self.runtime.get(row['id'])
        return (GrantBridgeClient(**job['config'], **options) if job else
                GrantBridgeClient(root, data_dir=data_dir, **options))

    def _owned(self, attempt_id, owner_ref, account_ref):
        if owner_ref:
            return self.store.get_auth_attempt(attempt_id, owner_ref)
        if account_ref:
            account = self.accounts.resolve(account_ref)
            row = self.store.latest_auth_attempt(account.id)
            if row:
                target = self.store.get_auth_attempt(attempt_id, row['owner'])
                if target['account_id'] == account.id:
                    return target
        raise BridgeError('authentication_owner_required', 'owner_ref is required for this authentication attempt.')

    @staticmethod
    def _owner_for(request_key):
        if not request_key:
            return uuid4().hex
        digest = hashlib.sha256(str(request_key).encode()).hexdigest()[:32]
        return 'request-' + digest

    def _save_remote(self, row, remote):
        row = self.store.get_auth_attempt(row['id'], row['owner'])
        if row['status'] in TERMINAL | {'bound', 'usable'}:
            return row
        return self.store.update_auth_attempt(row['id'], row['owner'],
                                              status=self._status(remote, row['status']),
                                              data=remote)

    @staticmethod
    def _status(remote, current=None):
        status = remote.get('status') if isinstance(remote, dict) else None
        if status in TERMINAL:
            return status
        verification = remote.get('verification') if isinstance(remote, dict) else {}
        if status == 'authorized' and not remote.get('checking') and isinstance(verification, dict) and verification.get('freshProcess') == 'passed':
            return 'verified'
        return status or current or 'unknown'

    @staticmethod
    def _public(row):
        data = row.get('data') or {}
        mapping = {
            'authorizationUrl': 'authorization_url', 'userCode': 'user_code',
            'createdAt': 'created_at', 'updatedAt': 'updated_at',
            'expiresAt': 'expires_at', 'probeError': 'probe_error',
        }
        result = {
            'attempt_id': row['id'], 'owner_ref': row['owner'],
            'account_ref': row['name'], 'engine': row['engine'],
            'status': row['status'],
        }
        for source, target in mapping.items():
            if source in data:
                result[target] = data[source]
        for key in ('provider', 'mode', 'browser', 'identity', 'verification',
                    'error', 'checking', 'auto_check'):
            if key in data:
                result[key] = data[key]
        return result
