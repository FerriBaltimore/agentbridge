"""Decide terminal run state without mistaking a lost stream for completion."""
from .provider_errors import normalize


def finish(parser, *, reason=None, exit_code=0, unresolved=False, stderr=''):
    engine = parser.engine
    if reason == 'user_stop':
        return 'cancelled', 'user_stop', None
    if reason:
        code = {'timeout': 'provider_timeout', 'protocol_error': 'provider_protocol_error'}.get(
            reason, 'unknown_outcome')
        return 'interrupted', code, normalize(engine, {'code': code}, outcome='unknown')
    if parser.terminal == 'interrupted':
        return 'interrupted', 'interrupted', normalize(engine, {'code': 'interrupted'})
    if exit_code != 0 or parser.failed:
        issue = parser.last_error or normalize(engine, stderr)
        if issue['code'] == 'provider_failed' and parser.terminal != 'failed':
            issue = normalize(engine, {'code': 'unknown_outcome'})
        if unresolved:
            issue = {**issue, 'outcome': 'unknown', 'action': 'inspect', 'retryable': False}
        issue = {**issue, 'terminal': True, 'provider_retrying': False}
        state = 'interrupted' if issue['code'] in {
            'provider_timeout', 'provider_connection_lost', 'provider_protocol_error', 'unknown_outcome'
        } else 'failed'
        return state, issue['code'], issue
    if parser.terminal != 'completed' or unresolved:
        issue = normalize(engine, {'code': 'unknown_outcome'})
        return 'incomplete' if unresolved else 'interrupted', issue['code'], issue
    return 'completed', None, None
