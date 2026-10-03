"""One account login: GrantBridge coordinates OAuth in a local CLIProxyAPI."""

import hashlib
import json
from uuid import uuid4

from . import auth_contract
from .auth_browser_channel import AuthBrowserChannel
from .auth_start import AuthStartMixin, PROVIDERS, TERMINAL
from .auth_identity import verify_or_fail_new_email
from .auth_proxy_binding import bind_proxy_account
from .auth_proxy_retirement import retire_terminal_proxy
from .auth_verification import recover_verified_identity, verification_data
from .auth_callback import validate_callback
from .errors import BridgeError
from .grantbridge import GrantBridgeClient
from .proxy import ManagementClient, ProxyRoute
from .proxy.credential_barrier import require_account
from .proxy.credential_reconnection import ensure_login_proxy



class AuthenticationService(AuthStartMixin, AuthBrowserChannel):
    def __init__(self, store, accounts, managed_proxy=None):
        self.store = store
        self.accounts = accounts
        self.managed_proxy = managed_proxy
        self._init_browser_channel()

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
        row = self._browser_recovery(recover_verified_identity(self, row))
        if row['status'] in TERMINAL | {'verified', 'bound', 'usable'}:
            retire_terminal_proxy(row, self.store, self.accounts, self.managed_proxy)
            return self._public(row)
        if not row['grantbridge_id']:
            raise BridgeError('authentication_attempt_not_ready', 'Authentication has not started.')
        route, connection = self._route(row)
        client = self._client(connection, grantbridge_root, data_dir)
        try:
            remote = client.proxy_status(row['grantbridge_id'], row['engine'],
                                         route['proxy_base_url'], route['management_key_env'])
        except BridgeError as error:
            if error.code in {'grantbridge_failed', 'grantbridge_timeout'}:
                self._lost_browser_client(client)
                if row['data'].get('browserTransport') == 'rpc':
                    return self._public(self.store.get_auth_attempt(attempt_id, row['owner']))
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
        remote = client.proxy_callback(
            row['grantbridge_id'], row['engine'], route['proxy_base_url'],
            route['management_key_env'], redirect_url)
        auth_contract.attempt(remote, engine=row['engine'],
                              attempt_id=row['grantbridge_id'])
        # The callback only delivered a code. Status observes whether the proxy exchanged it.
        return self._public(row)

    def check(self, attempt_id, *, owner_ref=None, account_ref=None,
              grantbridge_root=None, data_dir=None, inference=False):
        if inference:
            raise BridgeError('unsupported_operation', 'Proxy login checks do not start a model turn.')
        row = self._owned(attempt_id, owner_ref, account_ref)
        row = self._browser_recovery(recover_verified_identity(self, row))
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


    def _route(self, row):
        with self.store.connect() as db:
            require_account(db, row['account_id'], login=True)
        saved = self.store.auth_proxy_route(row['id'])
        route = saved['config']
        if self.managed_proxy and self.managed_proxy.is_managed(route, row['account_id']):
            ensure_login_proxy(self.store, self.managed_proxy,
                               row['account_id'], route['proxy_base_url'])
        return saved['config'], saved['connection']

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
                               ('browserTransport', 'browser_transport'),
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
