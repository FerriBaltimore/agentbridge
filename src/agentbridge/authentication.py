"""One account login: GrantBridge coordinates OAuth in a local CLIProxyAPI."""

import hashlib
import time
from uuid import uuid4

from . import auth_contract
from .auth_proxy_binding import bind_proxy_account
from .auth_callback import validate_callback
from .errors import BridgeError
from .grantbridge import GrantBridgeClient
from .models import account_name_key, identifier
from .proxy import ManagementClient, ProxyRoute


TERMINAL = {'failed', 'cancelled', 'expired', 'interrupted', 'abandoned', 'revoked', 'replaced'}
PROVIDERS = ('codex', 'claude', 'grok')


class AuthenticationService:
    def __init__(self, store, accounts, managed_proxy=None):
        self.store = store
        self.accounts = accounts
        self.managed_proxy = managed_proxy

    def start(self, *, provider, name, proxy_base_url=None, key_env=None,
              management_key_env=None, email=None, grantbridge_root=None, data_dir=None,
              mode='browser', browser='same_host', request_key=None, owner_ref=None):
        if provider not in PROVIDERS:
            raise BridgeError('invalid_provider', 'Choose a supported proxy provider.')
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise BridgeError('invalid_name', 'Account name must contain 1-128 non-space characters.')
        if mode != 'browser' or browser != 'same_host':
            raise BridgeError('unsupported_operation', 'Proxy login currently supports a local browser.')
        if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 256):
            raise BridgeError('invalid_request', 'request_key must contain 1-256 characters.')
        connection = GrantBridgeClient(grantbridge_root, data_dir=data_dir).configuration()
        owner = owner_ref or self._owner_for(request_key)
        identifier(owner)
        accounts = self.accounts.list()
        existing = next((item for item in accounts
                         if item.name and account_name_key(item.name) == account_name_key(name)), None)
        if existing and existing.provider != provider:
            raise BridgeError('account_migration_required',
                              'The existing account belongs to a different provider.')
        account_id = existing.id if existing else uuid4().hex
        supplied = (proxy_base_url, key_env, management_key_env)
        managed_created = not existing and not any(value is not None for value in supplied)
        if any(value is not None for value in supplied) and not all(value is not None for value in supplied):
            raise BridgeError('invalid_proxy_route', 'Provide all proxy route references or let AgentBridge manage them.')
        if not any(value is not None for value in supplied):
            if self.managed_proxy is None:
                raise BridgeError('proxy_unavailable', 'Managed CLIProxyAPI is unavailable.')
            if request_key:
                replay = self.store.auth_attempt_for_request(owner, request_key)
                if replay is not None:
                    expected = (provider, existing.name if existing else name, email, mode, browser)
                    actual = tuple(replay.get(field) for field in ('engine', 'name', 'email', 'mode', 'browser'))
                    if actual != expected:
                        raise BridgeError('idempotency_conflict',
                                          'Request key already belongs to different authentication input.')
                    return self._public(replay)
            if existing:
                if not self.managed_proxy.is_managed(existing.to_dict(), account_id):
                    raise BridgeError('account_migration_required',
                                      'This account uses an externally configured proxy route.')
                config = self.managed_proxy.ensure(account_id, existing.proxy_base_url)
            else:
                config = self.managed_proxy.provision(account_id)
            proxy_base_url = config['proxy_base_url']
            key_env = config['key_env']
            management_key_env = config['management_key_env']
        if key_env == management_key_env:
            raise BridgeError('invalid_environment', 'Proxy client and management keys must use different environment variables.')
        route = ProxyRoute(account_id, proxy_base_url, key_env)
        if any(item.id != account_id and item.proxy_base_url == route.base_url for item in accounts):
            raise BridgeError('proxy_endpoint_shared', 'The proxy endpoint belongs to another account.')
        management = ManagementClient(route, management_key_env)
        config = {'proxy_base_url': proxy_base_url, 'key_env': key_env,
                  'management_key_env': management_key_env}
        attempt = {'id': uuid4().hex, 'owner': owner, 'account_id': account_id,
                   'engine': provider, 'name': existing.name if existing else name,
                   'email': email, 'mode': mode, 'browser': browser, 'status': 'starting',
                   'data': {}}
        try:
            saved, created = self.store.create_auth_attempt(
                attempt, request_key=request_key, connection=connection, proxy_route=config)
        except BridgeError:
            if managed_created:
                try:
                    self.managed_proxy.retire(account_id)
                except BridgeError:
                    pass
            raise
        if not created:
            return self._public(saved)
        try:
            if existing:
                self._check_existing(existing, provider, config, management)
            else:
                management.ensure_empty()
        except BridgeError as error:
            self.store.update_auth_attempt(
                attempt['id'], owner, status='failed', data={'error': {'code': error.code}})
            if managed_created:
                try:
                    self.managed_proxy.retire(account_id)
                except BridgeError:
                    pass
            raise
        client = GrantBridgeClient(**connection)
        try:
            remote = client.proxy_start(provider, proxy_base_url, management_key_env)
            remote = auth_contract.attempt(remote, engine=provider)
            saved = self.store.update_auth_attempt(
                attempt['id'], owner, grantbridge_id=remote['id'],
                status=auth_contract.status(remote), data=remote)
        except BridgeError as error:
            # Once dispatched, an interrupted response cannot prove whether
            # CLIProxyAPI created an OAuth session. Do not retry automatically.
            self.store.update_auth_attempt(
                attempt['id'], owner, status='interrupted',
                data={'error': {'code': 'authentication_outcome_unknown'}})
            raise BridgeError('authentication_outcome_unknown',
                              'OAuth start has an unknown outcome. Abandon this attempt and use a new local proxy endpoint.') from error
        finally:
            client.close()
        return self._public(saved)

    def status(self, attempt_id, *, owner_ref=None, account_ref=None,
               grantbridge_root=None, data_dir=None):
        row = self._owned(attempt_id, owner_ref, account_ref)
        if row['status'] in TERMINAL | {'verified', 'bound'}:
            return self._public(row)
        if not row['grantbridge_id']:
            raise BridgeError('authentication_attempt_not_ready', 'Authentication has not started.')
        route, connection = self._route(row)
        client = self._client(connection, grantbridge_root, data_dir)
        try:
            try:
                remote = client.proxy_status(row['grantbridge_id'], row['engine'],
                                             route['proxy_base_url'], route['management_key_env'])
            except BridgeError as error:
                if error.code != 'authentication_outcome_unknown':
                    raise
                row = self.store.update_auth_attempt(
                    attempt_id, row['owner'], status='interrupted',
                    data={'error': {'code': error.code}})
                return self._public(row)
            remote = auth_contract.attempt(remote, engine=row['engine'], attempt_id=row['grantbridge_id'])
            row = self.store.update_auth_attempt(attempt_id, row['owner'],
                                                 status=auth_contract.status(remote),
                                                 data={**row['data'], **remote})
        finally:
            client.close()
        return self._public(row)

    def callback(self, attempt_id, *, owner_ref=None, redirect_url):
        """Relay a remote browser redirect through the same owned login attempt."""
        row = self._owned(attempt_id, owner_ref, None)
        if row['status'] not in {'starting', 'awaiting_user', 'exchanging'}:
            raise BridgeError('authentication_attempt_not_ready',
                              'The OAuth attempt is not waiting for a callback.')
        if not row['grantbridge_id']:
            raise BridgeError('authentication_attempt_not_ready',
                              'The OAuth attempt has not started.')
        validate_callback(row['engine'], row['grantbridge_id'], redirect_url)
        route, connection = self._route(row)
        client = self._client(connection)
        try:
            remote = client.proxy_callback(
                row['grantbridge_id'], row['engine'], route['proxy_base_url'],
                route['management_key_env'], redirect_url)
            auth_contract.attempt(remote, engine=row['engine'],
                                  attempt_id=row['grantbridge_id'])
        finally:
            client.close()
        # The callback only delivered a code. Status observes whether the proxy exchanged it.
        return self._public(row)

    def check(self, attempt_id, *, owner_ref=None, account_ref=None,
              grantbridge_root=None, data_dir=None, inference=False):
        if inference:
            raise BridgeError('unsupported_operation', 'Proxy login checks do not start a model turn.')
        row = self._owned(attempt_id, owner_ref, account_ref)
        if row['status'] == 'verified':
            return self._public(row)
        if row['status'] in TERMINAL | {'bound'}:
            raise BridgeError('authentication_required', 'This login cannot be checked.')
        current = self.status(attempt_id, owner_ref=row['owner'],
                              grantbridge_root=grantbridge_root, data_dir=data_dir)
        if current['status'] != 'authorized':
            raise BridgeError('authentication_attempt_not_ready', 'Authorize the proxy login before checking it.')
        row = self.store.get_auth_attempt(attempt_id, row['owner'])
        route, _ = self._route(row)
        observed = ManagementClient(
            ProxyRoute(row['account_id'], route['proxy_base_url'], route['key_env']),
            route['management_key_env']).observe()
        self._verify_observation(row, observed)
        data = {**row['data'], 'verification': {'proxyBinding': 'passed'},
                'proxy_binding': {'binding_fingerprint': observed['binding_fingerprint'],
                                  'identity_fingerprint': observed['identity_fingerprint']}}
        row = self.store.update_auth_attempt(attempt_id, row['owner'], status='verified', data=data)
        return self._public(row)

    def complete(self, attempt_id, *, owner_ref=None, account_ref=None,
                 grantbridge_root=None, data_dir=None):
        row = self._owned(attempt_id, owner_ref, account_ref)
        if row['status'] == 'bound':
            if row['account_id'] in self.store.retired_account_ids():
                raise BridgeError('authentication_required',
                                  'This account was removed. Start a new proxy login.')
            account = self.accounts.get(row['account_id'])
            return {'account': account.to_dict(), 'attempt': self._public(row),
                    'identity': row['data'].get('identity', {})}
        if row['status'] != 'verified':
            raise BridgeError('authentication_not_verified', 'Verify the proxy account before activation.')
        route, _ = self._route(row)
        observed = ManagementClient(
            ProxyRoute(row['account_id'], route['proxy_base_url'], route['key_env']),
            route['management_key_env']).observe()
        self._verify_observation(row, observed)
        checked = row['data'].get('proxy_binding') or {}
        if any(checked.get(key) != observed.get(key) for key in
               ('binding_fingerprint', 'identity_fingerprint')):
            raise BridgeError('proxy_binding_changed', 'The proxy credential changed after verification.')
        account, row = bind_proxy_account(self.store, row, route, observed)
        return {'account': account.to_dict(), 'attempt': self._public(row),
                'identity': row['data'].get('identity', {})}

    def cancel(self, attempt_id, *, owner_ref=None, account_ref=None,
               grantbridge_root=None, data_dir=None):
        row = self._owned(attempt_id, owner_ref, account_ref)
        if row['status'] == 'interrupted':
            # The remote outcome is unknown. This only abandons local ownership;
            # it must never claim that the proxy cancelled OAuth.
            row = self.store.update_auth_attempt(attempt_id, row['owner'], status='abandoned')
            return self._public(row)
        if row['status'] in TERMINAL | {'bound'}:
            return self._public(row)
        if row['status'] in {'authorized', 'verified'}:
            raise BridgeError('already_finished',
                              'OAuth already completed; the proxy credential remains available for activation.')
        if not row['grantbridge_id']:
            self.store.update_auth_attempt(
                attempt_id, row['owner'], status='interrupted',
                data={'error': {'code': 'authentication_outcome_unknown'}})
            raise BridgeError('authentication_outcome_unknown',
                              'OAuth start may be in progress. Abandon this attempt and use a new local proxy endpoint.')
        if row['grantbridge_id']:
            route, connection = self._route(row)
            client = self._client(connection, grantbridge_root, data_dir)
            try:
                try:
                    remote = client.proxy_cancel(row['grantbridge_id'], row['engine'],
                                                 route['proxy_base_url'], route['management_key_env'])
                except BridgeError as error:
                    if error.code != 'authentication_outcome_unknown':
                        raise
                    self.store.update_auth_attempt(
                        attempt_id, row['owner'], status='interrupted',
                        data={'error': {'code': error.code}})
                    raise BridgeError('authentication_outcome_unknown',
                                      'Proxy cancellation has an unknown outcome. Abandon this attempt and use a new local proxy endpoint.') from error
                remote = auth_contract.attempt(remote, engine=row['engine'],
                                               attempt_id=row['grantbridge_id'])
                outcome = auth_contract.status(remote)
            finally:
                client.close()
            if outcome == 'authorized':
                self.store.update_auth_attempt(attempt_id, row['owner'], status='authorized')
                raise BridgeError('already_finished',
                                  'OAuth already completed; the proxy credential remains available for activation.')
            if outcome == 'failed':
                return self._public(self.store.update_auth_attempt(
                    attempt_id, row['owner'], status='failed', data=remote))
            if outcome != 'cancelled':
                raise BridgeError('provider_protocol_error', 'The proxy did not confirm cancellation.')
        row = self.store.update_auth_attempt(attempt_id, row['owner'], status='cancelled')
        if self.managed_proxy and not any(
                account.id == row['account_id'] for account in self.accounts.list()):
            route = self.store.auth_proxy_route(row['id'])['config']
            if self.managed_proxy.is_managed(route, row['account_id']):
                try:
                    self.managed_proxy.retire(row['account_id'])
                except BridgeError:
                    pass
        return self._public(row)

    def login(self, *, provider, name, proxy_base_url=None, key_env=None, management_key_env=None,
              email=None, grantbridge_root=None, data_dir=None, mode='browser',
              browser='same_host', timeout=600, poll_interval=1.0, on_attempt=None,
              inference=False):
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 86400:
            raise BridgeError('invalid_timeout', 'Login timeout must be in [1, 86400] seconds.')
        if not isinstance(poll_interval, (int, float)) or not 0.05 <= poll_interval <= 60:
            raise BridgeError('invalid_poll_interval', 'Login poll interval must be in [0.05, 60] seconds.')
        attempt = self.start(
            provider=provider, name=name, proxy_base_url=proxy_base_url, key_env=key_env,
            management_key_env=management_key_env, email=email, grantbridge_root=grantbridge_root,
            data_dir=data_dir, mode=mode, browser=browser)
        if on_attempt:
            on_attempt(attempt)
        started = time.monotonic()
        while attempt['status'] not in TERMINAL | {'authorized', 'verified', 'bound'}:
            if time.monotonic() - started >= timeout:
                self.cancel(attempt['attempt_id'], owner_ref=attempt['owner_ref'])
                raise BridgeError('login_timeout', 'The proxy login timed out.')
            time.sleep(poll_interval)
            attempt = self.status(attempt['attempt_id'], owner_ref=attempt['owner_ref'])
            if on_attempt:
                on_attempt(attempt)
        if attempt['status'] in TERMINAL:
            if attempt['status'] == 'interrupted' and attempt.get('error', {}).get('code') == 'authentication_outcome_unknown':
                raise BridgeError('authentication_outcome_unknown',
                                  'The proxy OAuth result is unknown. Abandon this attempt and use a new local proxy endpoint.')
            raise BridgeError('login_failed', 'The provider did not complete authentication.')
        self.check(attempt['attempt_id'], owner_ref=attempt['owner_ref'], inference=inference)
        return self.complete(attempt['attempt_id'], owner_ref=attempt['owner_ref'])

    def _check_existing(self, account, provider, config, management):
        if (not account.proxy_base_url or account.engine != 'codex'
                or account.provider != provider
                or any(getattr(account, key) != value for key, value in config.items())):
            raise BridgeError('account_migration_required', 'This account cannot be changed by proxy login.')
        binding = self.store.proxy_binding(account.id)
        if binding is None:
            raise BridgeError('account_migration_required', 'The existing proxy account has no verified binding.')
        if management.credential_count() == 1:
            observed = management.observe()
            if observed['identity_fingerprint'] != binding['identity_fingerprint']:
                raise BridgeError('identity_changed', 'The local proxy belongs to another account.')

    def _verify_observation(self, row, observed):
        if (observed.get('provider') != row['engine'] or observed.get('status') != 'active'
                or observed.get('disabled') is not False
                or observed.get('unavailable') is not False or not observed.get('models')):
            raise BridgeError('proxy_binding_unverified', 'The authenticated proxy account is not usable.')
        if row.get('email') and row['email'] != observed.get('email'):
            raise BridgeError('identity_changed', 'The proxy identity does not match the requested email.')
        existing = next((account for account in self.accounts.list() if account.id == row['account_id']), None)
        if existing:
            binding = self.store.proxy_binding(existing.id)
            if binding is None or binding['identity_fingerprint'] != observed['identity_fingerprint']:
                raise BridgeError('identity_changed', 'Reauthentication cannot replace the account identity.')

    def _route(self, row):
        saved = self.store.auth_proxy_route(row['id'])
        route = saved['config']
        if self.managed_proxy and self.managed_proxy.is_managed(route, row['account_id']):
            self.managed_proxy.ensure(row['account_id'], route['proxy_base_url'])
        return saved['config'], saved['connection']

    @staticmethod
    def _client(connection, root=None, data_dir=None):
        return GrantBridgeClient(**connection) if connection else GrantBridgeClient(root, data_dir=data_dir)

    def _owned(self, attempt_id, owner_ref, account_ref):
        if owner_ref:
            return self.store.get_auth_attempt(attempt_id, owner_ref)
        if account_ref:
            account = self.accounts.resolve(account_ref)
            row = self.store.latest_auth_attempt(account.id)
            if row and row['id'] == attempt_id:
                return row
        raise BridgeError('authentication_owner_required', 'owner_ref is required for this login.')

    @staticmethod
    def _owner_for(request_key):
        if not request_key:
            return uuid4().hex
        return 'request-' + hashlib.sha256(request_key.encode()).hexdigest()[:32]

    @staticmethod
    def _status(remote, current=None):
        return auth_contract.status(remote)

    @staticmethod
    def _public(row):
        data = auth_contract.projection(row.get('data'))
        result = {'attempt_id': row['id'], 'owner_ref': row['owner'],
                  'account_ref': row['name'], 'provider': row['engine'],
                  'status': row['status']}
        for source, target in (('authorizationUrl', 'authorization_url'),
                               ('userCode', 'user_code'), ('createdAt', 'created_at'),
                               ('updatedAt', 'updated_at'), ('expiresAt', 'expires_at')):
            if source in data:
                result[target] = data[source]
        for key in ('identity', 'verification', 'error'):
            if key in data:
                result[key] = data[key]
        return result
