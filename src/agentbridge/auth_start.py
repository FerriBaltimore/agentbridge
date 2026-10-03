"""Start and synchronous orchestration for the existing proxy authorization flow."""

import time
from uuid import uuid4

from . import auth_contract
from .auth_entry import entry_failure, entry_params, validate_entry
from .auth_identity import check_existing
from .auth_proxy_retirement import retire_managed_proxy, retire_terminal_proxy
from .errors import BridgeError
from .models import account_name_key, identifier
from .proxy import ManagementClient, ProxyRoute
from .proxy.credential_barrier import require_account
from .proxy.credential_reconnection import ensure_login_proxy

PROVIDERS = ('codex', 'claude', 'grok')
TERMINAL = {'failed', 'cancelled', 'expired', 'interrupted', 'abandoned', 'revoked', 'replaced'}
KNOWN_START_FAILURES = frozenset({
    'credential_unavailable', 'invalid_environment', 'grantbridge_unavailable',
    'oauth_callback_port_busy', 'oauth_callback_unavailable',
    'invalid_browser', 'hosted_browser_unavailable',
})


class AuthStartMixin:
    def start(self, *, provider, name, proxy_base_url=None, key_env=None,
              management_key_env=None, email=None, grantbridge_root=None, data_dir=None,
              mode='browser', browser='same_host', request_key=None, owner_ref=None):
        from .authentication import GrantBridgeClient

        if provider not in PROVIDERS:
            raise BridgeError('invalid_provider', 'Choose a supported proxy provider.')
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise BridgeError('invalid_name',
                              'Account name must contain 1-128 non-space characters.')
        if email is not None and (not isinstance(email, str) or not 1 <= len(email) <= 320
                                  or email.strip() != email or '@' not in email
                                  or any(ord(char) <= 32 or ord(char) >= 127 for char in email)):
            raise BridgeError('invalid_request',
                              'email must be a valid account email when supplied.')
        validate_entry(mode, browser)
        if request_key is not None and (not isinstance(request_key, str)
                                        or not 1 <= len(request_key) <= 256):
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
        if any(value is not None for value in supplied) and not all(
                value is not None for value in supplied):
            raise BridgeError('invalid_proxy_route',
                              'Provide all proxy route references or let AgentBridge manage them.')
        if not any(value is not None for value in supplied):
            if self.managed_proxy is None:
                raise BridgeError('proxy_unavailable', 'Managed CLIProxyAPI is unavailable.')
            if request_key:
                replay = self.store.auth_attempt_for_request(owner, request_key)
                if replay is not None:
                    expected = (provider, existing.name if existing else name, email, mode, browser)
                    actual = tuple(replay.get(field)
                                   for field in ('engine', 'name', 'email', 'mode', 'browser'))
                    if actual != expected:
                        raise BridgeError('idempotency_conflict',
                                          'Request key already belongs to different '
                                          'authentication input.')
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
            raise BridgeError('invalid_environment',
                              'Proxy client and management keys must use different variables.')
        route = ProxyRoute(account_id, proxy_base_url, key_env)
        if any(item.id != account_id and item.proxy_base_url == route.base_url
               for item in accounts):
            raise BridgeError('proxy_endpoint_shared',
                              'The proxy endpoint belongs to another account.')
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
        rejected = None
        try:
            client = self._client(connection)
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
                if remote.get('browserTransport') == 'rpc':
                    self._browser_attempts[saved['id']] = (client, owner)
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
                              'OAuth start has an unknown outcome. Abandon this attempt '
                              'and use a new local proxy endpoint.',
                              outcome='unknown',
                              details={'attempt_id': attempt['id'], 'owner_ref': owner}) from error
        if rejected:
            saved = self.store.update_auth_attempt(
                attempt['id'], owner, status='failed',
                data={'error': {'code': 'provider_protocol_error'}})
            retire_terminal_proxy(saved, self.store, self.accounts, self.managed_proxy)
            raise BridgeError('provider_protocol_error', rejected)
        retire_terminal_proxy(saved, self.store, self.accounts, self.managed_proxy)
        return self._public(saved)

    def login(self, *, provider, name, proxy_base_url=None, key_env=None, management_key_env=None,
              email=None, grantbridge_root=None, data_dir=None, mode='browser',
              browser='same_host', timeout=600, poll_interval=1.0, on_attempt=None,
              inference=False):
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 86400:
            raise BridgeError('invalid_timeout', 'Login timeout must be in [1, 86400] seconds.')
        if not isinstance(poll_interval, (int, float)) or not 0.05 <= poll_interval <= 60:
            raise BridgeError('invalid_poll_interval',
                              'Login poll interval must be in [0.05, 60] seconds.')
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
            if (attempt['status'] == 'interrupted' and attempt.get('error', {}).get('code')
                    == 'authentication_outcome_unknown'):
                raise BridgeError('authentication_outcome_unknown',
                                  'The proxy OAuth result is unknown. Abandon this attempt '
                                  'and use a new local proxy endpoint.')
            raise BridgeError('login_failed', 'The provider did not complete authentication.')
        self.check(attempt['attempt_id'], owner_ref=attempt['owner_ref'], inference=inference)
        return self.complete(attempt['attempt_id'], owner_ref=attempt['owner_ref'])

