"""Account, login and usage surface of the public Bridge."""

from .errors import BridgeError, UnsupportedError
from .models import Account, page_values


class ClientAccountsMixin:
    def register(self, account: Account):
        raise BridgeError('authentication_required',
                          'Create accounts through the proxy login flow.')

    def accounts(self, **filters):
        if filters.get('engine') is not None:
            raise BridgeError('unsupported_parameter',
                              'Accounts are selected by provider and model.')
        return self.account_service.list(**filters)

    def account(self, id):
        return self.account_service.get(id)

    def resolve_account(self, reference):
        return self.account_service.resolve(reference)

    def account_reference(self, account_id):
        return self.account_service.reference(account_id)

    def account_status(self, reference=None, *, account_id=None, account_ref=None,
                       refresh=False):
        if account_id is not None:
            if reference is not None or account_ref is not None:
                raise BridgeError('invalid_request', 'Provide one account_ref or account_id.')
            account = self.account_service.get(account_id)
        else:
            account = self.account(self._account_id(reference, account_ref))
        retirement = self.store.retirement_status(account.id)
        if retirement['retired']:
            return {**self.account_service.status(account.id, refresh=False),
                    'retirement': {**retirement, 'managed_proxy':
                                   self.managed_proxy.is_managed(account.to_dict(), account.id)}}
        if account.proxy_base_url:
            if refresh or self.managed_proxy.is_managed(account.to_dict(), account.id):
                self.routes.observation(account, refresh=refresh)
            return self.account_service.status(account.id, refresh=False)
        result = self.account_service.status(account.id, refresh=False)
        result['authentication'] = {**result['authentication'],
                                    'status': 'legacy_read_only'}
        result['reason'] = 'legacy_read_only'
        return result

    def account_usage(self, account_id=None, *, account_ref=None, refresh=False):
        account = self.account(self._account_id(account_id, account_ref))
        if account.id in self.store.retired_account_ids():
            return {'account_id': account.id, 'scope': 'account', 'supported': False,
                    'stale': True, 'reason': 'account_removed'}
        if account.proxy_base_url:
            return self.routes.usage(account, refresh=refresh)
        return {'account_id': account.id, 'scope': 'account', 'supported': False,
                'stale': True, 'reason': 'legacy_read_only'}

    def usage(self, scope='account', *, account_ref=None, instance_id=None, turn_id=None,
              refresh=False, include_quota=False, since=None, until=None):
        if since or until:
            raise UnsupportedError('Usage time filters are not supported by this adapter.')
        if scope == 'account':
            account_id = self._account_id(None, account_ref)
            value = self.account_usage(account_id, refresh=refresh)
            # Account usage already includes quota. Never replace a live snapshot
            # with the older rollout-only compatibility surface.
            return {**value, 'scope': 'account'}
        if scope == 'turn':
            if not turn_id:
                raise BridgeError('turn_id_required', 'turn_id is required for turn usage.')
            return {'scope': 'turn', 'turn_id': turn_id, **self.run(turn_id).consumption}
        if scope == 'instance':
            if not instance_id:
                raise BridgeError('instance_id_required',
                                  'instance_id is required for instance usage.')
            rows = self.store.session_runs(instance_id, limit=10001)
            partial = len(rows) > 10000
            rows = rows[:10000]
            observations = [self.run(row['id']).consumption for row in rows]
            return {'scope': 'instance', 'instance_id': instance_id,
                    'turns': len(rows), 'observations': observations,
                    'partial': partial, 'observation_limit': 10000,
                    'supported': any(item.get('supported') for item in observations)}
        raise BridgeError('invalid_scope', 'scope must be account, instance or turn.')

    def account_usage_history(self, account_id, *, limit=100, cursor=0, since=None,
                              until=None, granularity=None, refresh=False):
        limit, cursor = page_values(limit, cursor)
        if since or until or granularity:
            raise UnsupportedError('Historical usage filters are not supported by this adapter.')
        values = self.account_service.history(account_id, limit=limit + cursor)
        return values[cursor:cursor + limit]

    def account_quota_reset(self, account_ref, *, idempotency_key, credit_id=None):
        raise UnsupportedError('The local proxy does not expose Codex earned reset redemption.')

    def account_login_browser(self, attempt_id, **options):
        return self.authentication.browser(attempt_id, **options)

    def account_login(self, **options):return self.authentication.login(**options)
    def account_login_attempts(self, **options):return self.authentication.attempts(**options)
    def account_login_start(self, **options):return self.authentication.start(**options)
    def account_login_check(self, attempt_id=None, **options):
        return self.authentication.check(attempt_id or options.pop('attempt_id'), **options)
    def account_login_complete(self, attempt_id=None, **options):
        return self.authentication.complete(attempt_id or options.pop('attempt_id'), **options)
    def account_login_status(self, attempt_id=None, **options):
        return self.authentication.status(attempt_id or options.pop('attempt_id'), **options)
    def account_login_cancel(self, attempt_id=None, **options):
        return self.authentication.cancel(attempt_id or options.pop('attempt_id'), **options)
    def account_login_callback(self, attempt_id=None, **options):
        return self.authentication.callback(attempt_id or options.pop('attempt_id'), **options)

    def _account_id(self, account_id, account_ref):
        if account_id or account_ref:
            return self.account_service.resolve(account_ref or account_id,
                                                include_retired=True).id
        raise BridgeError('account_ref_required', 'account_ref is required.')

