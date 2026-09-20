"""Classify provider failures in memory and expose only bounded, safe metadata."""
import re

from .errors import BridgeError


NATIVE_CODES = {
    'context_window_exceeded': 'context_window_exceeded',
    'context_length_exceeded': 'context_window_exceeded',
    'session_budget_exceeded': 'budget_exhausted',
    'usage_limit_exceeded': 'quota_exhausted',
    'usage_limit': 'quota_exhausted',
    'insufficient_quota': 'quota_exhausted',
    'rate_limit_exceeded': 'rate_limited',
    'rate_limit_error': 'rate_limited',
    'rate_limit': 'rate_limited',
    'resource_exhausted': 'rate_limited',
    'server_overloaded': 'provider_unavailable',
    'overloaded_error': 'provider_unavailable',
    'cyber_policy': 'safety_blocked',
    'misalignment_policy_violation': 'safety_blocked',
    'content_filter': 'safety_blocked',
    'content_policy_violation': 'safety_blocked',
    'internal_server_error': 'provider_unavailable',
    'server_error': 'provider_unavailable',
    'api_error': 'provider_failed',
    'unauthorized': 'authentication_required',
    'unauthenticated': 'authentication_required',
    'api_key_not_found': 'authentication_required',
    'authentication_failed': 'authentication_required',
    'authentication_error': 'authentication_required',
    'cloud_credential_error': 'authentication_required',
    'oauth_org_not_allowed': 'authorization_denied',
    'account_on_hold': 'authorization_denied',
    'verification_required': 'authentication_required',
    'permission_error': 'authorization_denied',
    'permission_denied': 'authorization_denied',
    'role_forbidden': 'authorization_denied',
    'forbidden': 'authorization_denied',
    'billing_error': 'billing_required',
    'overloaded': 'provider_unavailable',
    'model_not_found': 'model_unavailable',
    'invalid_model': 'model_unavailable',
    'bad_request': 'invalid_request',
    'invalid_request_error': 'invalid_request',
    'invalid_argument': 'invalid_request',
    'http_connection_failed': 'provider_connection_lost',
    'response_stream_connection_failed': 'provider_connection_lost',
    'response_stream_disconnected': 'provider_connection_lost',
    'response_too_many_failed_attempts': 'provider_connection_lost',
    'unavailable': 'provider_connection_lost',
    'data_loss': 'provider_connection_lost',
    'timeout': 'provider_timeout',
    'deadline_exceeded': 'provider_timeout',
    'max_output_tokens': 'output_limit_exceeded',
    'max_tokens': 'output_limit_exceeded',
    'error_max_turns': 'max_turns_exceeded',
    'error_max_budget_usd': 'budget_exhausted',
    'error_max_structured_output_retries': 'structured_output_failed',
    'aborted_streaming': 'interrupted',
    'aborted_tools': 'interrupted',
    'cancelled': 'interrupted',
    'canceled': 'interrupted',
    'expired': 'interrupted',
}
CANONICAL = set(NATIVE_CODES.values()) | {
    'provider_failed', 'provider_protocol_error', 'unknown_outcome',
    'cursor_sdk_unavailable', 'worker_failed', 'provider_error',
    'unsupported_parameter', 'provider_catalog_unsupported', 'unsupported',
}
UNKNOWN_OUTCOMES = {'provider_timeout', 'provider_connection_lost', 'provider_protocol_error',
                    'unknown_outcome', 'interrupted', 'worker_failed'}
CODE_FIELDS = ('codexErrorInfo', 'proto_error_code', 'sdkErrorCode', 'code', 'type',
               'subtype', 'terminal_reason', 'error')
TEXT_FIELDS = ('message', 'error', 'errors', 'result', 'additionalDetails', 'text')


def _code(value):
    if not isinstance(value, str):
        return None
    value = value.removeprefix('SDK_ERROR_CODE_')
    return re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', value).lower()


def _structured(value, depth=0):
    if isinstance(value, str):
        name = _code(value)
        mapped = NATIVE_CODES.get(name) or (name if name in CANONICAL else None)
        return (mapped, name) if mapped else (None, None)
    if not isinstance(value, dict) or depth > 3:
        return None, None
    for key in CODE_FIELDS:
        candidate = value.get(key)
        if isinstance(candidate, dict) and key == 'codexErrorInfo':
            candidate = next(iter(candidate), None)
        code, native = _structured(candidate, depth + 1)
        if code and code not in {'provider_failed', 'provider_error'}:
            return code, native
    return _structured(value.get('data'), depth + 1)


def _texts(value, depth=0):
    if isinstance(value, str):
        return value[:16384]
    if depth > 3:
        return ''
    if isinstance(value, dict):
        return ' '.join(_texts(value.get(key), depth + 1) for key in TEXT_FIELDS)[:16384]
    if isinstance(value, list):
        return ' '.join(_texts(item, depth + 1) for item in value[:16])[:16384]
    return ''


def _text_code(value):
    text = _texts(value).lower()
    if any(s in text for s in ('blocked by our safety systems', 'potentially unintended activity',
                               'content policy violation', 'safety policy violation')):
        return 'safety_blocked'
    if 'not your usage limit' in text or 'rate limit' in text or 'too many requests' in text:
        return 'rate_limited'
    if any(s in text for s in ('usage limit', 'quota exhausted', 'insufficient quota',
                               'quota exceeded', 'limit reached', 'out of usage')):
        return 'quota_exhausted'
    if any(s in text for s in ('context window exceeded', 'context length exceeded',
                               'prompt is too long', 'maximum context length')):
        return 'context_window_exceeded'
    if any(s in text for s in ('unauthorized', 'authentication required', 'not logged in',
                               'invalid api key', 'authentication failed')):
        return 'authentication_required'
    if 'timed out' in text or 'timeout' in text:
        return 'provider_timeout'
    if any(s in text for s in ('stream disconnected', 'stream closed', 'connection reset',
                               'connection closed', 'network error')):
        return 'provider_connection_lost'
    return None


def _http_status(value, depth=0):
    if not isinstance(value, dict) or depth > 4:
        return None
    for key in ('httpStatusCode', 'status_code', 'status', 'api_error_status'):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and 400 <= number < 600:
            return number
    for key in ('error', 'data', 'codexErrorInfo', 'httpConnectionFailed',
                'responseStreamConnectionFailed', 'responseStreamDisconnected',
                'responseTooManyFailedAttempts'):
        number = _http_status(value.get(key), depth + 1)
        if number:
            return number
    return None


def normalize(engine, value, *, terminal=True, outcome=None, phase='execution', provider_retrying=False):
    """No provider text, arbitrary codes, headers or continuation instructions escape."""
    code, native = _structured(value)
    detection = 'structured' if code else 'unclassified'
    status = _http_status(value)
    text_code = _text_code(value)
    if text_code == 'safety_blocked' or (code == 'invalid_request' and text_code):
        code, detection = text_code, 'text_match'
    if code == 'provider_connection_lost' and status in {401, 403, 429}:
        code = {401: 'authentication_required', 403: 'authorization_denied', 429: 'rate_limited'}[status]
    if not code:
        code = text_code
        detection = 'text_match' if code else 'unclassified'
    if not code and status:
        code = {401: 'authentication_required', 403: 'authorization_denied',
                402: 'billing_required', 408: 'provider_timeout', 429: 'rate_limited'}.get(status)
        if status >= 500:
            code = 'provider_unavailable'
        detection = 'http_status' if code else detection
    code = code or 'provider_failed'
    if outcome is None:
        outcome = 'unknown' if code in UNKNOWN_OUTCOMES or not terminal else 'failed'
    details = {'detection': detection}
    if native:
        details['provider_code'] = native
    if status:
        details['http_status'] = status
    previous = value.get('details') if isinstance(value, dict) else None
    if isinstance(previous, dict):
        candidate = previous.get('provider_code')
        if isinstance(candidate, str) and candidate in NATIVE_CODES and NATIVE_CODES[candidate] == code:
            details['provider_code'] = candidate
        if previous.get('detection') in {'structured', 'text_match', 'http_status', 'unclassified'}:
            details['detection'] = previous['detection']
        number = previous.get('http_status')
        if isinstance(number, int) and not isinstance(number, bool) and 400 <= number < 600:
            details['http_status'] = number
    data = BridgeError(code, 'The provider operation did not complete.', phase=phase,
                       outcome=outcome, retryable=False, details=details).safe_data()
    return {**data, 'engine': engine, 'terminal': bool(terminal),
            'provider_retrying': bool(provider_retrying)}


def exception(engine, error):
    """Cursor SDK exposes structured exception fields, never copy details or headers."""
    value = {key: getattr(error, key, None) for key in ('code', 'proto_error_code', 'status')}
    value['message'] = str(error)[:16384]
    if isinstance(error, TimeoutError):
        value['code'] = 'provider_timeout'
    return normalize(engine, value, outcome='unknown')
