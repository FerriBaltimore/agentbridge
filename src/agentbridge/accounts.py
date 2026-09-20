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
from .quota_windows import project, timestamp


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
        return project(account.engine, self._usage(account, refresh=refresh))

    def _usage(self, account, *, refresh=False):
        if refresh:
            observation = self._probe_account(account, include_usage=True)
            data = observation['data'].get('usage')
            if data is not None:
                return data
        cached = self.store.latest_usage_observation(account.id)
        if cached:
            return {'account_id': account.id, 'observed_at': stamp(cached['observed_at']),
                    'source': cached['source'], 'scope': cached['scope'],
                    **cached['data'], 'stale': cached['stale']}
        if account.engine == 'codex':
            data = usage.snapshot(account)
            self.store.usage_observation(account.id, data.get('source', 'codex_rollout'), 'account', data,
                                         stale=bool(data.get('outdated', True)),
                                         observed_at=timestamp(data.get('observed_at')))
            return {'account_id': account.id, 'observed_at': data.get('observed_at'),
                    'source': data.get('source', 'codex_rollout'), 'scope': 'account',
                    'stale': bool(data.get('outdated', True)), **data}
        return {'account_id': account.id, 'engine': account.engine, 'supported': False,
                'source': None, 'scope': 'account', 'stale': True,
                'reason': 'sdk_account_quota_unavailable' if account.engine == 'cursor' else 'account_usage_unsupported'}

    def history(self, account_id, *, limit=100):
        account = self.resolve(account_id)
        rows = self.store.usage_history(account.id, limit=limit)
        return [{**row, 'data': project(account.engine, {**row['data'],
                 'source': row['source'], 'observed_at': stamp(row['observed_at']),
                 'stale': row['stale']})} for row in rows]

    def _probe_account(self, account, *, include_usage):
        if account.engine == 'claude':
            return self._claude_account(account, include_usage=include_usage)
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
                          'account_usage': result.get('account_usage'),
                          'quota_reason': result.get('quota_reason'),
                          'account_usage_reason': result.get('account_usage_reason')}
            self.store.usage_observation(account.id, 'codex_app_server', 'account', usage_data, stale=False)
            result['usage'] = {'account_id': account.id, 'observed_at': stamp(), 'source': 'codex_app_server',
                               'scope': 'account', 'stale': False, **usage_data}
        elif include_usage:
            # Failed refresh must not leave an older snapshot looking current.
            cached = self.store.latest_usage_observation(account.id)
            if cached:
                with self.store.connect() as db:
                    db.execute('UPDATE usage_observations SET stale=1 WHERE id=?', (cached['id'],))
            result['usage'] = {'account_id': account.id,
                **(cached['data'] if cached else {'supported': False, 'scope': 'account'}),
                'stale': True, 'reason': result.get('reason') or result.get('quota_reason') or 'quota_not_reported',
                'observed_at': stamp(cached['observed_at']) if cached else None}
        return {'data': result, 'status': result['status']}

    def _claude_account(self, account, *, include_usage):
        from .claude_account import identity, quota
        try:
            result = identity(account)
            if include_usage and result['status'] != 'loaded_only':
                raise BridgeError(result['status'], 'The bound profile cannot refresh usage.')
            if include_usage and result['status'] == 'loaded_only':
                data = quota(account)
                self.store.usage_observation(account.id, data['source'], 'account', data, stale=False)
                result['usage'] = {'account_id': account.id, 'observed_at': stamp(), **data}
        except BridgeError as error:
            result = {'status': error.code, 'identity': {}, 'reason': error.code}
            if include_usage:
                cached = self.store.latest_usage_observation(account.id)
                if cached:
                    with self.store.connect() as db:
                        db.execute('UPDATE usage_observations SET stale=1 WHERE id=?', (cached['id'],))
                result['usage'] = {'account_id': account.id,
                    **(cached['data'] if cached else {'supported': False, 'scope': 'account'}),
                    'stale': True, 'reason': error.code,
                    'observed_at': stamp(cached['observed_at']) if cached else None}
        self.store.account_observation(account.id, 'claude_native_status', result['status'], result)
        return {'data': result, 'status': result['status']}

    def _cursor_binding(self, account):
        reference = account.credential_ref
        try:
            with GrantBridgeClient(**reference['connection']) as client:
                value = client.activate(reference['attempt_id'], reference['owner_ref'])
            if (not isinstance(value, dict) or value.get('attempt_id') != reference['attempt_id']):
                raise BridgeError('provider_protocol_error', 'The credential binding response is incompatible.')
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
