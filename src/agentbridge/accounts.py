"""Read-only account registry for the single GrantBridge and proxy flow.

Authentication creates accounts only after the local proxy binding is verified.
This service resolves saved account metadata and observations; it never probes a
native provider or creates an account directly.
"""

import json
import time

from .account_probe import stamp
from .errors import BridgeError
from .models import Account, account_name_key, identifier, page_values
from .quota_windows import project


class AccountService:
    """Resolve account references and project persisted observations."""

    def __init__(self, store):
        self.store = store

    def register(self, account):
        raise BridgeError('authentication_required', 'Create accounts through the proxy login flow.')

    def get(self, account_id):
        account_id = identifier(account_id)
        row = self.store.get('accounts', account_id)
        try:
            return Account(**json.loads(row['config']))
        except (BridgeError, TypeError, ValueError):
            raise BridgeError('account_unavailable',
                              'The saved account configuration is incompatible.') from None

    def resolve(self, reference, *, include_retired=False):
        """Resolve only references that identify one eligible account."""
        retired = self.store.retired_account_ids()
        if isinstance(reference, str) and reference.startswith('id:'):
            try:
                account = self.get(reference[3:])
            except BridgeError as error:
                if error.code in ('invalid_id', 'not_found'):
                    raise BridgeError('account_not_found',
                                      'No account matches that reference.') from None
                raise
            else:
                if account.id not in retired or include_retired:
                    return account
                raise BridgeError('account_not_found', 'No account matches that reference.')
        active_matches = {}
        retired_matches = {}
        try:
            exact = self.get(reference)
        except BridgeError as error:
            if error.code not in ('invalid_id', 'not_found'):
                raise
        else:
            if exact.id in retired:
                if include_retired:
                    retired_matches[exact.id] = exact
            else:
                active_matches[exact.id] = exact
        if isinstance(reference, str):
            wanted_name = account_name_key(reference)
            for account in self.list():
                if account.name and account_name_key(account.name) == wanted_name:
                    active_matches[account.id] = account
            if include_retired:
                for row in self.store.list('accounts'):
                    if row['id'] not in retired:
                        continue
                    try:
                        account = Account(**json.loads(row['config']))
                    except (BridgeError, TypeError, ValueError):
                        continue
                    if account.name and account_name_key(account.name) == wanted_name:
                        retired_matches[account.id] = account
        if len(active_matches) > 1:
            raise BridgeError('invalid_request',
                              'Account reference is ambiguous; use an internal account ID.')
        if active_matches:
            account = next(iter(active_matches.values()))
            # A reused human name identifies the active account. If only the
            # active ID matches a retired name, an untyped ref is ambiguous.
            if include_retired and retired_matches and (not account.name or
                    account_name_key(account.name) != account_name_key(reference)):
                raise BridgeError('invalid_request',
                                  'Account reference is ambiguous; use an internal account ID.')
            return account
        if include_retired and len(retired_matches) > 1:
            raise BridgeError('invalid_request',
                              'Account reference is ambiguous; use an internal account ID.')
        if retired_matches:
            return next(iter(retired_matches.values()))
        raise BridgeError('account_not_found', 'No account matches that reference.')

    def reference(self, account_id):
        """Give colliding names a stable, explicitly typed account reference."""
        account = self.get(account_id)
        candidate = account.name or account.id
        if candidate.startswith('id:'):
            return f'id:{account.id}'
        key = account_name_key(candidate)
        for other in self.list():
            if other.id == account.id:
                continue
            if (other.id == candidate or other.name
                    and account_name_key(other.name) == key):
                return f'id:{account.id}'
        return candidate

    def list(self, *, engine=None, authentication=None, limit=None, cursor=0):
        limit, cursor = page_values(limit, cursor, allow_none=True)
        retired = self.store.retired_account_ids()
        accounts = []
        for row in self.store.list('accounts'):
            if row['id'] in retired:
                continue
            try:
                accounts.append(Account(**json.loads(row['config'])))
            except (BridgeError, TypeError, ValueError):
                continue
        if engine:
            accounts = [account for account in accounts if account.engine == engine]
        if authentication:
            allowed = {authentication} if isinstance(authentication, str) else set(authentication)
            observed = {row['account_id']: row for row in self.store.list_account_observations()}
            accounts = [account for account in accounts
                        if observed.get(account.id, {}).get('status') in allowed]
        return accounts[cursor:] if limit is None else accounts[cursor:cursor + limit]

    def status(self, account_id, *, refresh=False):
        if refresh:
            raise BridgeError('unsupported_operation', 'Account observations refresh through the local proxy.')
        account = self.get(account_id)
        retired = account.id in self.store.retired_account_ids()
        observation = self.store.latest_account_observation(account.id)
        configured = {'name': account.name, 'email': account.email,
                      'provider': account.provider, 'supported_models': list(account.supported_models),
                      'credential_ref': bool(account.env_names or account.key_env or account.home or account.credential_ref)}
        if not observation:
            return {'account_id': account.id, 'configured': configured,
                    'authentication': {'status': 'retired' if retired else 'not_observed',
                                       'source': None, 'observed_at': None},
                    'identity': {}, 'reason': 'account_removed' if retired else 'no_observation'}
        result = {'account_id': account.id, 'configured': configured,
                'authentication': {'status': observation['status'], 'source': observation['source'],
                                   'observed_at': stamp(observation['observed_at'])},
                **observation['data']}
        if retired:
            result['authentication']['status'] = 'retired'
            result['reason'] = 'account_removed'
        return result

    def history(self, account_id, *, limit=100):
        account = self.resolve(account_id, include_retired=True)
        rows = self.store.usage_history(account.id, limit=limit)
        result = []
        for row in rows:
            if row['source'] in {'cliproxy_management', 'cliproxy_upstream_usage'}:
                from .proxy.quota import project as project_quota
                age = time.time() - row['observed_at']
                windows = project_quota(row['data'].get('quota_windows'))
                stale = (row['stale'] or not 0 <= age < 60 or
                         not any(not item['stale'] and item.get('used_percent') is not None
                                 for item in windows))
                data = {**row['data'], 'account_id': account.id,
                        'source': row['source'], 'scope': row['scope'],
                        'observed_at': stamp(row['observed_at']), 'stale': stale,
                        'quota_windows': windows,
                        'age_seconds': round(age, 3) if age >= 0 else None}
                result.append({**row, 'stale': stale, 'data': data})
            else:
                data = project(account.engine, {**row['data'],
                    'source': row['source'], 'observed_at': stamp(row['observed_at']),
                    'stale': row['stale']})
                result.append({**row, 'data': data})
        return result
