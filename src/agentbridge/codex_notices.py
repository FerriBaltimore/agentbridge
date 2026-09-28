"""Recognize pinned app-server lifecycle notices without copying provider prose."""

from .errors import BridgeError


NOTICES = {
    'configWarning': ('configuration_warning', 'summary', None),
    'warning': ('warning', 'message', None),
    'deprecationNotice': ('deprecation_warning', 'summary', None),
    'remoteControl/status/changed': (
        'remote_control', 'status', {'disabled', 'connecting', 'connected', 'errored'}),
    'mcpServer/startupStatus/updated': (
        'mcp_startup', 'status', {'starting', 'ready', 'failed', 'cancelled'}),
}


def notice(method, params):
    """Return typed metadata for known warnings/status, never message completion."""
    if method == 'thread/goal/cleared':
        if not isinstance(params.get('threadId'), str) or not params['threadId']:
            raise BridgeError('provider_protocol_error', 'Invalid native goal notice.',
                              phase='execution', outcome='unknown')
        return {'type': 'bridge_notice', 'kind': 'goal', 'status': 'cleared'}
    specification = NOTICES.get(method)
    if specification is None:
        return None
    kind, field, states = specification
    value = params.get(field)
    if not isinstance(value, str) or states is not None and value not in states:
        raise BridgeError('provider_protocol_error', 'Invalid native runtime notice.',
                          phase='execution', outcome='unknown')
    return {'type': 'bridge_notice', 'kind': kind,
            'status': value if states is not None else 'warning'}
