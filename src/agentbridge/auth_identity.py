"""Identity checks of an observed proxy credential against the login attempt."""

from .auth_proxy_retirement import retire_terminal_proxy
from .errors import BridgeError


def check_existing(store, account, provider, config, management):
    """An existing proxy account may only be re-authenticated as the same identity."""
    if (not account.proxy_base_url or account.engine != 'codex'
            or account.provider != provider
            or any(getattr(account, key) != value for key, value in config.items())):
        raise BridgeError('account_migration_required', 'This account cannot be changed by proxy login.')
    binding = store.proxy_binding(account.id)
    if binding is None:
        raise BridgeError('account_migration_required', 'The existing proxy account has no verified binding.')
    if management.credential_count() == 1:
        observed = management.observe()
        if observed['identity_fingerprint'] != binding['identity_fingerprint']:
            raise BridgeError('identity_changed', 'The local proxy belongs to another account.')


def verify_observation(store, accounts, row, observed):
    if (observed.get('provider') != row['engine'] or observed.get('status') != 'active'
            or observed.get('disabled') is not False
            or observed.get('unavailable') is not False or not observed.get('models')):
        raise BridgeError('proxy_binding_unverified', 'The authenticated proxy account is not usable.')
    expected_email, observed_email = row.get('email'), observed.get('email')
    if expected_email and (not isinstance(expected_email, str)
                           or not isinstance(observed_email, str)
                           or expected_email.casefold() != observed_email.casefold()):
        raise BridgeError('identity_changed', 'The proxy identity does not match the requested email.')
    existing = next((account for account in accounts.list() if account.id == row['account_id']), None)
    if existing:
        binding = store.proxy_binding(existing.id)
        if binding is None or binding['identity_fingerprint'] != observed['identity_fingerprint']:
            raise BridgeError('identity_changed', 'Reauthentication cannot replace the account identity.')


def verify_or_fail_new_email(store, accounts, managed_proxy, row, observed):
    """A new account authenticated as a different email fails terminally and releases its proxy.

    The failed attempt keeps the observed identity email so the host can tell the person
    which account the provider actually returned; the credential itself is retired.
    """
    try:
        verify_observation(store, accounts, row, observed)
    except BridgeError as error:
        expected, actual = row.get('email'), observed.get('email')
        if (error.code == 'identity_changed' and isinstance(expected, str) and expected
                and isinstance(actual, str) and '@' in actual
                and actual.casefold() != expected.casefold()
                and not any(account.id == row['account_id'] for account in accounts.list())):
            saved = store.update_auth_attempt(
                row['id'], row['owner'], status='failed',
                data={**row['data'], 'verification': {'proxyBinding': 'failed'},
                      'identity': {'email': actual},
                      'error': {'code': 'identity_changed'}})
            retire_terminal_proxy(saved, store, accounts, managed_proxy)
        raise
