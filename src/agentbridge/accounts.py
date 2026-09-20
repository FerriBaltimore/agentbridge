"""Account identity and usage service, independent from process supervision.

The service never stores credential values. Provider probes return bounded,
observable facts only. A missing observation is different from an empty quota.
"""
import json
from . import usage
from .account_probe import CodexAppServerProbe, stamp, safe_identity
from .grantbridge import GrantBridgeClient
from .errors import BridgeError
from .models import Account, account_name_key, identifier, page_values


class AccountService:
    """Account registry, identity health and provider usage observations."""

    def __init__(self, store):
        self.store = store

    def register(self, account):
        self.store.account(account)
        return self.get(account.id)

    def get(self, account_id):
        account_id = identifier(account_id)
        row = self.store.get('accounts', account_id)
        return Account(**json.loads(row['config']))

    def resolve(self, reference):
        """Resolve a human-facing unique name or an internal account ID."""
        if isinstance(reference, str):
            wanted_name = account_name_key(reference)
            for account in self.list():
                if account.name and account_name_key(account.name) == wanted_name:
                    return account
        try:
            return self.get(reference)
        except BridgeError as error:
            if error.code in ('invalid_id', 'not_found'):
                raise BridgeError('account_not_found', 'No account matches that name.') from None
            raise

    def list(self, *, engine=None, authentication=None, limit=None, cursor=0):
        limit, cursor = page_values(limit, cursor, allow_none=True)
        accounts = [Account(**json.loads(row['config'])) for row in self.store.list('accounts')]
        if engine:
            accounts = [account for account in accounts if account.engine == engine]
        if authentication:
            allowed = {authentication} if isinstance(authentication, str) else set(authentication)
            observed = {row['account_id']: row for row in self.store.list_account_observations()}
            accounts = [account for account in accounts
                        if observed.get(account.id, {}).get('status') in allowed]
        return accounts[cursor:] if limit is None else accounts[cursor:cursor + limit]

    def status(self, account_id, *, refresh=False):
        account = self.get(account_id)
        observation = self.store.latest_account_observation(account.id)
        if refresh:
            self._probe_account(account, include_usage=False)
            observation = self.store.latest_account_observation(account.id)
        configured = {'name': account.name, 'email': account.email, 'engine': account.engine,
                      'credential_ref': bool(account.env_names or account.key_env or account.home or account.credential_ref)}
        if not observation:
            return {'account_id': account.id, 'configured': configured,
                    'authentication': {'status': 'not_observed', 'source': None, 'observed_at': None},
                    'identity': {}, 'reason': 'no_observation'}
        return {'account_id': account.id, 'configured': configured,
                'authentication': {'status': observation['status'], 'source': observation['source'],
                                   'observed_at': stamp(observation['observed_at'])},
                **observation['data']}

    def usage(self, account_id, *, refresh=False):
        account = self.get(account_id)
        if refresh:
            observation = self._probe_account(account, include_usage=True)
            data = observation['data'].get('usage')
            if data is not None:
                return data
        cached = self.store.latest_usage_observation(account.id)
        if cached:
            return {'account_id': account.id, 'observed_at': stamp(cached['observed_at']),
                    'source': cached['source'], 'scope': cached['scope'],
                    'stale': cached['stale'], **cached['data']}
        if account.engine == 'codex':
            data = usage.snapshot(account)
            self.store.usage_observation(account.id, data.get('source', 'codex_rollout'), 'account', data,
                                         stale=bool(data.get('outdated', True)))
            return {'account_id': account.id, 'observed_at': data.get('observed_at'),
                    'source': data.get('source', 'codex_rollout'), 'scope': 'account',
                    'stale': bool(data.get('outdated', True)), **data}
        return {'account_id': account.id, 'engine': account.engine, 'supported': False,
                'source': None, 'scope': 'account', 'stale': True,
                'reason': 'account_usage_unsupported'}

    def history(self, account_id, *, limit=100):
        self.get(account_id)
        return self.store.usage_history(account_id, limit=limit)

    def _probe_account(self, account, *, include_usage):
        if account.engine == 'cursor' and account.credential_ref:
            return self._cursor_binding(account)
        if account.engine != 'codex':
            data = {'status': 'unsupported', 'identity': {}, 'reason': 'provider_probe_unsupported'}
            self.store.account_observation(account.id, 'agentbridge', data['status'], data)
            return {'data': data, 'status': data['status']}
        try:
            result = CodexAppServerProbe(account).read(include_usage=include_usage)
        except BridgeError as error:
            result = {'status': error.code, 'identity': {}, 'reason': error.code}
        self.store.account_observation(account.id, CodexAppServerProbe.source, result['status'], result)
        if include_usage and (result.get('quota') is not None or result.get('account_usage') is not None):
            usage_data = {'supported': True, 'source': 'codex_app_server', 'quota': result.get('quota'),
                          'account_usage': result.get('account_usage')}
            self.store.usage_observation(account.id, 'codex_app_server', 'account', usage_data, stale=False)
            result['usage'] = {'account_id': account.id, 'observed_at': stamp(), 'source': 'codex_app_server',
                               'scope': 'account', 'stale': False, **usage_data}
        return {'data': result, 'status': result['status']}

    def _cursor_binding(self, account):
        reference = account.credential_ref
        try:
            with GrantBridgeClient(**reference['connection']) as client:
                value = client.activate(reference['attempt_id'], reference['owner_ref'])
            identity = safe_identity(value.get('identity'))
            if value.get('provider') != 'cursor' or (account.email and identity.get('email') != account.email):
                raise BridgeError('identity_changed', 'The account binding no longer matches its identity.')
            result = {'status': 'usable', 'identity': identity,
                      'reason': 'cached_provider_verification', 'live_request': False,
                      'credential_expires_at_ms': value.get('expires_at_ms')}
        except BridgeError as error:
            result = {'status': 'authentication_required', 'identity': {}, 'reason': error.code}
        self.store.account_observation(account.id, 'grantbridge_binding', result['status'], result)
        return {'data': result, 'status': result['status']}
