"""GrantBridge private adapter projections, separate from local auth states.

Source: GrantBridge src/agentbridge-protocol.mjs and src/agentbridge-proxy.mjs.
Additive fields are ignored. Unknown states or malformed evidence never verify a login.
"""
from .account_probe import safe_identity
from .errors import BridgeError
from .models import finite_number

REMOTE_STATES = frozenset(('starting', 'awaiting_user', 'exchanging', 'authorized',
                          'failed', 'cancelled', 'expired', 'interrupted', 'revoked', 'replaced'))
ERROR_CODES = frozenset(('invalid_request', 'invalid_params', 'invalid_provider', 'invalid_browser',
    'provider_busy', 'not_found', 'already_finished', 'not_ready', 'method_not_found',
    'authentication_required', 'authentication_not_verified', 'credential_unavailable',
    'credential_expired', 'identity_changed', 'activation_unsupported', 'provider_error',
    'native_reauthorization_required', 'plan_required', 'probe_failed', 'probe_interrupted',
    'codex_closed', 'claude_login_failed', 'no_code_expected', 'invalid_code',
    'authentication_interrupted', 'authentication_worker_failed', 'provider_protocol_error',
    'grantbridge_failed', 'grantbridge_timeout', 'invalid_proxy_endpoint',
    'proxy_unavailable', 'proxy_rejected', 'proxy_invalid_response',
    'oauth_callback_port_busy', 'oauth_callback_unavailable',
    'authentication_outcome_unknown'))


def invalid():
    raise BridgeError('provider_protocol_error', 'GrantBridge returned an incompatible authentication response.')


def error_code(value):
    return value if isinstance(value, str) and value in ERROR_CODES else 'grantbridge_failed'


def status(remote):
    if not isinstance(remote, dict) or not isinstance(remote.get('status'), str):
        invalid()
    value = remote['status']
    if value not in REMOTE_STATES:
        invalid()
    if 'checking' in remote and not isinstance(remote['checking'], bool):
        invalid()
    verification = remote.get('verification')
    if verification is not None and not isinstance(verification, dict):
        invalid()
    return value


def text(value, maximum=512):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value)


def projection(remote):
    """Closed nested projection; never persist opaque provider errors or future secrets."""
    if not isinstance(remote, dict):
        return {}
    result = {}
    for key in ('id', 'provider', 'mode', 'browser', 'status', 'userCode'):
        if text(remote.get(key)):
            result[key] = remote[key]
    # Authorization URL and user code are intentionally part of the protected login flow.
    if 'authorizationUrl' in remote and (remote['authorizationUrl'] is None
                                         or text(remote['authorizationUrl'], 16384)):
        result['authorizationUrl'] = remote.get('authorizationUrl')
    for key in ('createdAt', 'updatedAt', 'expiresAt'):
        value = remote.get(key)
        if finite_number(value) and value >= 0:
            result[key] = value
    for key in ('checking', 'autoCheck', 'autoChecked', 'manualCodeRequired'):
        if isinstance(remote.get(key), bool):
            result[key] = remote[key]
    if isinstance(remote.get('identity'), dict):
        result['identity'] = safe_identity(remote['identity'])
    verification = remote.get('verification')
    if isinstance(verification, dict):
        result['verification'] = {key: value for key in ('proxyBinding',)
            if (value := verification.get(key)) in ('passed', 'failed', 'loaded_only')}
    for key in ('error', 'probeError'):
        value = remote.get(key)
        if isinstance(value, dict):
            result[key] = {'code': error_code(value.get('code'))}
    return result


def attempt(remote, *, engine, attempt_id=None):
    status(remote)
    if (not text(remote.get('id'), 256) or remote.get('provider') != engine
            or (attempt_id is not None and remote['id'] != attempt_id)):
        invalid()
    value = projection(remote)
    # Only AgentBridge's local Management API observation can verify a binding.
    value.pop('verification', None)
    value.pop('identity', None)
    return value


def response_result(response):
    """Validate the JSON-RPC envelope and keep unknown private error codes opaque."""
    if (isinstance(response.get('id'), bool) or response.get('jsonrpc') != '2.0'
            or ('result' in response) == ('error' in response)):
        invalid()
    if 'error' not in response:
        return response['result']
    error = response['error']
    if (not isinstance(error, dict) or not isinstance(error.get('code'), int)
            or isinstance(error['code'], bool)):
        invalid()
    data = error.get('data')
    code = error_code(data.get('code') if isinstance(data, dict) else None)
    raise BridgeError(code, 'GrantBridge did not complete the operation.')
