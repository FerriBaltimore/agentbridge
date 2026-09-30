"""One account login: GrantBridge coordinates OAuth in a local CLIProxyAPI."""

import hashlib
import json
import time
from uuid import uuid4

from . import auth_contract
from .auth_entry import entry_failure, entry_params, validate_entry
from .auth_identity import check_existing, verify_or_fail_new_email
from .auth_proxy_binding import bind_proxy_account
from .auth_proxy_retirement import retire_managed_proxy, retire_terminal_proxy
from .auth_verification import recover_verified_identity, verification_data
from .auth_callback import validate_callback
from .errors import BridgeError
from .grantbridge import GrantBridgeClient
from .models import account_name_key, identifier
from .proxy import ManagementClient, ProxyRoute
from .proxy.credential_barrier import require_account
from .proxy.credential_reconnection import ensure_login_proxy


TERMINAL = {'failed', 'cancelled', 'expired', 'interrupted', 'abandoned', 'revoked', 'replaced'}
PROVIDERS = ('codex', 'claude', 'grok')
KNOWN_START_FAILURES = frozenset({
    'credential_unavailable', 'invalid_environment', 'grantbridge_unavailable',
    'oauth_callback_port_busy', 'oauth_callback_unavailable',
    'invalid_browser', 'hosted_browser_unavailable',
})


class AuthenticationService:
    def __init__(self, store, accounts, managed_proxy=None):
        self.store = store
        self.accounts = accounts
        self.managed_proxy = managed_proxy

    def attempts(self, *, provider=None, limit=100, cursor=0):
        """List only interrupted local attempts for explicit owner recovery."""
        if provider is not None and provider not in PROVIDERS:
            raise BridgeError('invalid_provider', 'Choose a supported proxy provider.')
        if type(limit) is not int or not 1 <= limit <= 100:
            raise BridgeError('invalid_limit', 'limit must be in [1, 100].')
        if type(cursor) is not int or not 0 <= cursor <= 10000:
            raise BridgeError('invalid_cursor', 'cursor must be in [0, 10000].')
        rows = self.store.interrupted_auth_attempts(provider, limit=limit, cursor=cursor)
        return [{'attempt_id': row['id'], 'owner_ref': row['owner'],
                 'provider': row['engine'], 'account_ref': row['name'],
                 'status': 'interrupted', 'created_at': row['created'],
                 'error': {'code': auth_contract.error_code(
                     (json.loads(row['data']).get('error') or {}).get('code'))}}
                for row in rows]

    def start(self, *, provider, name, proxy_base_url=None, key_env=None,
              management_key_env=None, email=None, grantbridge_root=None, data_dir=None,
              mode='browser', browser='same_host', request_key=None, owner_ref=None):
        if provider not in PROVIDERS:
            raise BridgeError('invalid_provider', 'Choose a supported proxy provider.')
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise BridgeError('invalid_name', 'Account name must contain 1-128 non-space characters.')
        if email is not None and (not isinstance(email, str) or not 1 <= len(email) <= 320
                                  or email.strip() != email or '@' not in email
                                  or any(ord(char) <= 32 or ord(char) >= 127 for char in email)):
            raise BridgeError('invalid_request', 'email must be a valid account email when supplied.')
        validate_entry(mode, browser)
        if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 256):
            raise BridgeError('invalid_request', 'request_key must contain 1-256 characters.')
        connection = GrantBridgeClient(grantbridge_root, data_dir=data_dir,
                                       state_root=self.store.root).configuration()
        owner = owner_ref or self._owner_for(request_key)
        identifier(owner)
        accounts = self.accounts.list()
        matching = [item for item in accounts if (item.provider or item.engine) == provider
                    and item.name and account_name_key(item.name) == account_name_key(name)]
        if len(matching) > 1:
            raise BridgeError('account_name_in_use',
                              'Multiple accounts with this name already exist for the provider.')
        existing = matching[0] if matching else None
        account_id = existing.id if existing else uuid4().hex
        with self.store.connect() as db:
            require_account(db, account_id, login=True)
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
                config = ensure_login_proxy(self.store, self.managed_proxy,
                                            account_id, existing.proxy_base_url)
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
        except BridgeError as error:
            if managed_created:
                retire_managed_proxy(self.managed_proxy, account_id,
                                     prior_error_code=error.code)
            raise
        if not created:
            return self._public(saved)
        try:
            if existing:
                check_existing(self.store, existing, provider, config, management)
            else:
                management.ensure_empty()
        except BridgeError as error:
            saved = self.store.update_auth_attempt(
                attempt['id'], owner, status='failed', data={'error': {'code': error.code}})
            retire_terminal_proxy(saved, self.store, self.accounts, self.managed_proxy)
            raise
        client = None
        rejected = None
        try:
            client = GrantBridgeClient(**connection)
            remote = client.proxy_start(provider, proxy_base_url, management_key_env,
                                        **entry_params(browser, mode, owner))
            remote = auth_contract.attempt(remote, engine=provider)
            rejected = entry_failure(remote, browser, mode)
            if rejected:
                # GrantBridge answered without honouring the phone entry; release its session.
                self._abandon(client, remote['id'], provider, proxy_base_url, management_key_env)
            else:
                saved = self.store.update_auth_attempt(
                    attempt['id'], owner, grantbridge_id=remote['id'],
                    status=auth_contract.status(remote), data=remote)
        except BridgeError as error:
            if error.code in KNOWN_START_FAILURES:
                # These failures happen before the request reaches CLIProxyAPI.
                saved = self.store.update_auth_attempt(
                    attempt['id'], owner, status='failed',
                    data={'error': {'code': error.code}})
                retire_terminal_proxy(saved, self.store, self.accounts, self.managed_proxy)
                raise
            # Once dispatched, an interrupted response cannot prove whether
            # CLIProxyAPI created an OAuth session. Do not retry automatically.
            self.store.update_auth_attempt(
                attempt['id'], owner, status='interrupted',
                data={'error': {'code': 'authentication_outcome_unknown'}})
            raise BridgeError('authentication_outcome_unknown',
                              'OAuth start has an unknown outcome. Abandon this attempt and use a new local proxy endpoint.',
                              outcome='unknown',
                              details={'attempt_id': attempt['id'], 'owner_ref': owner}) from error
        finally:
            if client is not None:
                client.close()
        if rejected:
            saved = self.store.update_auth_attempt(
                attempt['id'], owner, status='failed',
                data={'error': {'code': 'provider_protocol_error'}})
            retire_terminal_proxy(saved, self.store, self.accounts, self.managed_proxy)
            raise BridgeError('provider_protocol_error', rejected)
        retire_terminal_proxy(saved, self.store, self.accounts, self.managed_proxy)
        return self._public(saved)

    @staticmethod
    def _abandon(client, state, provider, base_url, management_key_env):
        """Best-effort release of a proxy OAuth session the phone can never finish."""
        try:
            client.proxy_cancel(state, provider, base_url, management_key_env)
        except BridgeError:
            pass

    def status(self, attempt_id, *, owner_ref=None, account_ref=None,
               grantbridge_root=None, data_dir=None):
        row = self._owned(attempt_id, owner_ref, account_ref)
        row = recover_verified_identity(self, row)
        if row['status'] in TERMINAL | {'verified', 'bound', 'usable'}:
            retire_terminal_proxy(row, self.store, self.accounts, self.managed_proxy)
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
        retire_terminal_proxy(row, self.store, self.accounts, self.managed_proxy)
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
        row = recover_verified_identity(self, row)
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
        verify_or_fail_new_email(self.store, self.accounts, self.managed_proxy, row, observed)
        data = verification_data(row, observed)
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
        verify_or_fail_new_email(self.store, self.accounts, self.managed_proxy, row, observed)
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
            retire_terminal_proxy(row, self.store, self.accounts, self.managed_proxy)
            return self._public(row)
        if row['status'] in TERMINAL | {'bound'}:
            retire_terminal_proxy(row, self.store, self.accounts, self.managed_proxy)
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
                row = self.store.update_auth_attempt(
                    attempt_id, row['owner'], status='failed', data=remote)
                retire_terminal_proxy(row, self.store, self.accounts, self.managed_proxy)
                return self._public(row)
            if outcome != 'cancelled':
                raise BridgeError('provider_protocol_error', 'The proxy did not confirm cancellation.')
        row = self.store.update_auth_attempt(attempt_id, row['owner'], status='cancelled')
        retire_terminal_proxy(row, self.store, self.accounts, self.managed_proxy)
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

    def _route(self, row):
        with self.store.connect() as db:
            require_account(db, row['account_id'], login=True)
        saved = self.store.auth_proxy_route(row['id'])
        route = saved['config']
        if self.managed_proxy and self.managed_proxy.is_managed(route, row['account_id']):
            ensure_login_proxy(self.store, self.managed_proxy,
                               row['account_id'], route['proxy_base_url'])
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

    def _public(self, row):
        data = auth_contract.projection(row.get('data'))
        reference = (self.accounts.reference(row['account_id'])
                     if row['status'] in {'bound', 'usable'} else row['name'])
        result = {'attempt_id': row['id'], 'owner_ref': row['owner'],
                  'account_ref': reference, 'provider': row['engine'],
                  'status': row['status'], 'mode': row['mode'], 'browser': row['browser']}
        if row['status'] in {'bound', 'usable'}:
            result['account_id'] = row['account_id']
        for source, target in (('authorizationUrl', 'authorization_url'),
                               ('userCode', 'user_code'), ('viewerUrl', 'viewer_url'),
                               ('createdAt', 'created_at'),
                               ('updatedAt', 'updated_at'), ('expiresAt', 'expires_at')):
            if source in data:
                result[target] = data[source]
        # The hosted browser opens the provider; the phone must only open the viewer page.
        if row['mode'] == 'hosted':
            result.pop('authorization_url', None)
        for key in ('identity', 'verification', 'error'):
            if key in data:
                result[key] = data[key]
        return result
