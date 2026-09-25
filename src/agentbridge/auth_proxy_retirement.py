"""Confirm shutdown of an unused managed sidecar after terminal OAuth."""

from .errors import BridgeError


_CONFIRMED_STOP = {'retired': True, 'upstream_credential_removed': False}
_SAFE_TERMINAL = frozenset({'failed', 'expired', 'cancelled'})


def retire_managed_proxy(managed_proxy, account_id, *, attempt=None, prior_error_code=None):
    """Report an unverified stop without claiming credential removal."""
    details = {'account_id': account_id}
    if attempt is not None:
        details.update(attempt_id=attempt['id'], owner_ref=attempt['owner'])
    if prior_error_code is not None:
        details['prior_error_code'] = prior_error_code
    try:
        stopped = managed_proxy.retire(account_id)
    except Exception:
        raise BridgeError('managed_proxy_stop_unverified',
                          'The local proxy could not be confirmed stopped.',
                          phase='execution', outcome='unknown', details=details) from None
    if stopped != _CONFIRMED_STOP:
        raise BridgeError('managed_proxy_stop_unverified',
                          'The local proxy could not be confirmed stopped.',
                          phase='execution', outcome='unknown', details=details)


def retire_terminal_proxy(row, store, accounts, managed_proxy):
    """Stop only an unbound proxy after a confirmed terminal auth result."""
    if row['status'] not in _SAFE_TERMINAL or managed_proxy is None:
        return
    if any(account.id == row['account_id'] for account in accounts.list()):
        return
    config = store.auth_proxy_route(row['id'])['config']
    if managed_proxy.is_managed(config, row['account_id']):
        retire_managed_proxy(managed_proxy, row['account_id'], attempt=row)
