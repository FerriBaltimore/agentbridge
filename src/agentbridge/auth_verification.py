"""Publish authoritative verified identity and recover older private login records."""

from .account_probe import safe_identity
from .auth_identity import verify_observation
from .errors import BridgeError
from .proxy.management import ManagementClient
from .proxy.route import ProxyRoute


def verification_data(row, observed):
    return {**row['data'], 'identity': safe_identity({'email': observed.get('email')}),
            'verification': {'proxyBinding': 'passed'},
            'proxy_binding': {'binding_fingerprint': observed['binding_fingerprint'],
                              'identity_fingerprint': observed['identity_fingerprint']}}


def recover_verified_identity(authentication, row):
    """Recover only omitted identity, without changing the already verified binding."""
    if row['status'] != 'verified' or 'identity' in row['data']:
        return row
    route, _ = authentication._route(row)
    observed = ManagementClient(
        ProxyRoute(row['account_id'], route['proxy_base_url'], route['key_env']),
        route['management_key_env']).observe()
    # An observation mismatch must not mutate or retire the verified credential.
    verify_observation(authentication.store, authentication.accounts, row, observed)
    checked = row['data'].get('proxy_binding') or {}
    if any(checked.get(key) != observed.get(key) for key in
           ('binding_fingerprint', 'identity_fingerprint')):
        raise BridgeError('proxy_binding_changed',
                          'The proxy credential changed after verification.')
    return authentication.store.update_auth_attempt(
        row['id'], row['owner'], status='verified', data=verification_data(row, observed))
