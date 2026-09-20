"""Resolve account references for execution without persisting credential values."""
import os

from .errors import BridgeError
from .grantbridge import GrantBridgeClient

CURSOR_KEY_ENV = 'AGENTBRIDGE_CURSOR_API_KEY'


def environment(account):
    values = {}
    for name in (*account.env_names, *((account.key_env,) if account.key_env else ())):
        if not os.environ.get(name):
            raise BridgeError('credential_unavailable', 'A configured credential is unavailable.')
        values[name] = os.environ[name]
    if account.credential_ref:
        reference = account.credential_ref
        with GrantBridgeClient(**reference['connection']) as client:
            result = client.credentials(reference['attempt_id'], reference['owner_ref'])
        if (not isinstance(result, dict) or result.get('provider') != account.engine
                or not isinstance(result.get('api_key'), str) or not result['api_key']):
            raise BridgeError('credential_unavailable', 'The credential resolver returned no usable credential.')
        values[CURSOR_KEY_ENV] = result['api_key']
    if account.engine == 'cursor' and not values.get(account.key_env or CURSOR_KEY_ENV):
        # Never let the SDK silently discover the operator's default login.
        raise BridgeError('credential_unavailable', 'Cursor requires an explicit account credential.')
    return values
