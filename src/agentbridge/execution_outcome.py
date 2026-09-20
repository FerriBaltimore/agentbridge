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
        issue = parser.last_error or normalize(engine, {'code': 'interrupted'})
        issue = {**issue, 'outcome': 'unknown', 'action': 'inspect', 'retryable': False,
                 'terminal': True, 'provider_retrying': False}
        return 'interrupted', issue['code'], issue
    if exit_code != 0 or parser.failed:
        issue = parser.last_error or normalize(engine, stderr)
        lost_unknown = (parser.terminal != 'failed' and
                        issue.get('details', {}).get('detection') in {'unclassified', 'learned_rule'})
        if issue['code'] == 'provider_failed' and lost_unknown:
            unknown = normalize(engine, {'code': 'unknown_outcome'})
            issue = {**unknown, 'details': issue['details']}
        if lost_unknown:
            issue = {**issue, 'outcome': 'unknown', 'action': 'inspect', 'retryable': False}
        if unresolved:
            issue = {**issue, 'outcome': 'unknown', 'action': 'inspect', 'retryable': False}
        issue = {**issue, 'terminal': True, 'provider_retrying': False}
        state_code = issue['details'].get('unclassified_code', issue['code']) if (
            issue['details'].get('detection') == 'learned_rule') else issue['code']
        state = 'interrupted' if lost_unknown or state_code in {
            'provider_timeout', 'provider_connection_lost', 'provider_protocol_error', 'unknown_outcome'
        } else 'failed'
        return state, issue['code'], issue
    if parser.terminal != 'completed' or unresolved:
        issue = normalize(engine, {'code': 'unknown_outcome'})
        return 'incomplete' if unresolved else 'interrupted', issue['code'], issue
    return 'completed', None, None
