"""Explicit earned-reset reads and redemptions for bound Codex proxy accounts."""

from datetime import datetime, timezone
import time

from .errors import BridgeError, UnsupportedError
from .proxy.management import ManagementClient
from .proxy.route import ProxyRoute


RESET_OBSERVATION_TTL = 60


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds')


def _opaque(value, label, maximum):
    if (not isinstance(value, str) or not 0 < len(value) <= maximum
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise BridgeError('invalid_request', f'{label} must be a non-empty opaque identifier.')
    return value


class AccountResetMixin:
    def _reset_account(self, account_ref, account_id):
        if (account_ref is None) == (account_id is None):
            raise BridgeError('invalid_request', 'Provide one account_ref or account_id.')
        account = (self.account_service.get(account_id) if account_id is not None else
                   self.account_service.resolve(account_ref))
        if self.store.retirement_status(account.id)['retired']:
            raise BridgeError('account_retired', 'The selected proxy account was retired.')
        if account.provider != 'codex' or not account.proxy_base_url:
            raise UnsupportedError('Earned rate-limit resets require a Codex proxy account.')
        if not self.store.proxy_login_origin(account):
            raise BridgeError('proxy_binding_unverified', 'This account has no completed GrantBridge proxy login.')
        binding = self.store.proxy_binding(account.id)
        if binding is None:
            raise BridgeError('proxy_binding_unverified', 'The Codex account binding is unavailable.')
        return account, binding

    def _reset_proxy(self, account, binding):
        from .proxy.reset_credits import CodexResetProxy

        if self.managed_proxy.is_managed(account.to_dict(), account.id):
            self.managed_proxy.ensure(account.id, account.proxy_base_url)
        route = ProxyRoute(account.id, account.proxy_base_url, account.key_env)
        client = ManagementClient(route, account.management_key_env, timeout=8)
        return CodexResetProxy(client, binding['binding_fingerprint'])

    def _reset_credit_view(self, account, *, reason=None):
        stored = self.store.reset_observation(account.id)
        pending = self.store.pending_reset_attempt(account.id)
        binding = self.store.proxy_binding(account.id)
        if binding is None or any(
                item is not None and any(item[key] != binding[key] for key in
                                         ('binding_fingerprint', 'identity_fingerprint'))
                for item in (stored, pending)):
            reason = 'proxy_binding_changed'
        age = time.time() - stored['observed_at'] if stored and stored['observed_at'] > 0 else None
        stale = stored is None or age is None or not 0 <= age < RESET_OBSERVATION_TTL
        value = stored['data'] if stored and reason != 'proxy_binding_changed' else {}
        return {
            'account_id': account.id,
            'account_ref': self.account_reference(account.id),
            'provider': 'codex',
            'source': 'codex_proxy_reset_credits' if stored else None,
            'status': value.get('status', 'unknown'),
            'available_count': value.get('available_count'),
            'credits': value.get('credits'),
            'observed_at': _stamp(stored['observed_at']) if age is not None else None,
            'age_seconds': round(age, 3) if age is not None and age >= 0 else None,
            'stale': stale or reason is not None,
            'observation_ref': stored['observation_ref'] if stored and reason != 'proxy_binding_changed' else None,
            'pending_reset': ({'idempotency_key': pending['idempotency_key'],
                               'observation_ref': pending['observation_ref'],
                               'credit_id': pending['credit_id']}
                              if pending else None),
            'reason': reason if reason is not None else 'not_observed' if stored is None else None,
        }

    def account_reset_credits(self, account_ref=None, *, account_id=None, refresh=False):
        """Read earned credits through the exact GrantBridge-bound Codex sidecar."""
        account, binding = self._reset_account(account_ref, account_id)
        if not refresh:
            return self._reset_credit_view(account)
        pending = self.store.pending_reset_attempt(account.id)
        if pending and any(pending[key] != binding[key] for key in
                           ('binding_fingerprint', 'identity_fingerprint')):
            return self._reset_credit_view(account, reason='proxy_binding_changed')
        try:
            data = self._reset_proxy(account, binding).read()
            self.store.save_reset_observation(account.id, binding, data)
        except BridgeError as error:
            self.store.expire_reset_observation(account.id)
            return self._reset_credit_view(account, reason=error.code)
        return self._reset_credit_view(account)

    def account_quota_reset(self, account_ref=None, *, account_id=None, idempotency_key,
                            observation_ref, credit_id=None):
        """Consume one earned reset only after a fresh, explicit account observation."""
        key = _opaque(idempotency_key, 'idempotency_key', 200)
        revision = _opaque(observation_ref, 'observation_ref', 128)
        if credit_id is not None:
            _opaque(credit_id, 'credit_id', 512)
        account, binding = self._reset_account(account_ref, account_id)
        reference = self.account_reference(account.id)
        attempt = self.store.begin_reset_attempt(account.id, key, revision, credit_id, binding,
                                                 maximum_age=RESET_OBSERVATION_TTL)
        if attempt['state'] == 'done':
            if attempt['outcome'] == 'not_started':
                raise BridgeError('reset_attempt_not_started',
                                  'Refresh reset credits before a new redemption attempt.')
            return {'account_id': account.id, 'account_ref': reference,
                    'outcome': attempt['outcome'], 'windows_reset': attempt['windows_reset'],
                    'reset_credits': self._reset_credit_view(account)}
        try:
            result = self._reset_proxy(account, binding).consume(key, credit_id)
        except BridgeError as error:
            if error.outcome == 'not_started' and attempt['started_now']:
                try:
                    self.store.finish_reset_attempt(
                        key, 'not_started', None,
                        dispatch_started=attempt['dispatch_started'])
                except Exception:
                    raise BridgeError('reset_outcome_unknown',
                                      'The Codex reset outcome is unknown.',
                                      phase='redemption', outcome='unknown') from None
                raise
            try:
                self.store.release_reset_attempt(key, attempt['dispatch_started'])
            except Exception:
                pass
            if error.outcome == 'not_started':
                raise BridgeError('reset_outcome_unknown', 'The Codex reset outcome is unknown.',
                                  phase='redemption', outcome='unknown') from None
            raise
        try:
            receipt = self.store.finish_reset_attempt(
                key, result['outcome'], result.get('windows_reset'))
        except Exception:
            try:
                self.store.release_reset_attempt(key, attempt['dispatch_started'])
            except Exception:
                pass
            raise BridgeError('reset_outcome_unknown', 'The Codex reset outcome is unknown.',
                              phase='redemption', outcome='unknown') from None
        outcome = receipt['outcome']
        windows_reset = receipt['windows_reset']
        if outcome == 'not_started':
            raise BridgeError('reset_outcome_unknown', 'The Codex reset outcome is unknown.',
                              phase='redemption', outcome='unknown')
        try:
            credits = self.account_reset_credits(account_id=account.id, refresh=True)
        except Exception:
            credits = {'account_id': account.id, 'account_ref': reference,
                       'provider': 'codex', 'status': 'unknown',
                       'available_count': None, 'credits': None, 'stale': True,
                       'reason': 'reset_credit_refresh_unavailable'}
        # A successful redemption invalidates earlier quota percentages. A fresh
        # read is useful, but its failure must never alter the known outcome.
        usage = None
        usage_refresh_reason = None
        if outcome in {'reset', 'already_redeemed'}:
            try:
                usage = self.account_usage(account.id, refresh=True)
            except Exception:
                usage_refresh_reason = 'usage_refresh_unavailable'
        return {'account_id': account.id, 'account_ref': reference,
                'outcome': outcome, 'windows_reset': windows_reset,
                'reset_credits': credits, 'usage': usage,
                'usage_refresh_reason': usage_refresh_reason}
