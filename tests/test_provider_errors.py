import json
import pytest

from agentbridge.codex_control import CodexControl
from agentbridge.execution_outcome import finish
from agentbridge.protocols import Parser
from agentbridge.provider_errors import exception, normalize


@pytest.mark.parametrize(('provider_code', 'code'), [
    ('usageLimitExceeded', 'quota_exhausted'),
    ('rateLimitExceeded', 'rate_limited'),
    ('contextWindowExceeded', 'context_window_exceeded'),
    ('sessionBudgetExceeded', 'budget_exhausted'),
    ('cyberPolicy', 'safety_blocked'),
    ('misalignmentPolicyViolation', 'safety_blocked'),
    ({'responseStreamDisconnected': {'httpStatusCode': 502}}, 'provider_connection_lost'),
    ({'httpConnectionFailed': {'httpStatusCode': 401}}, 'authentication_required'),
])
def test_codex_structured_failures_never_store_provider_text(provider_code, code):
    issue = normalize('codex', {'codexErrorInfo': provider_code, 'message': 'secret-body',
        'additionalDetails': 'secret-detail', 'misalignment': {'steer': {'message': 'secret-instruction'}}})
    assert issue['code'] == code
    assert issue['retryable'] is False
    assert 'secret-' not in json.dumps(issue)


def test_safety_block_is_not_quota_network_or_retryable():
    issue = normalize('codex', {
        'message': 'This request was blocked by our safety systems. Reason: Potentially unintended activity.',
        'request_headers': {'Authorization': 'secret-token'}})
    assert issue['code'] == 'safety_blocked'
    assert issue['category'] == 'safety'
    assert issue['action'] == 'inspect' and issue['retryable'] is False
    assert issue['details']['detection'] == 'text_match'
    assert 'secret-token' not in json.dumps(issue)


def test_rate_limit_does_not_become_usage_exhaustion():
    issue = normalize('claude', 'API rate limit reached, not your usage limit')
    assert issue['code'] == 'rate_limited'
    assert normalize('claude', {'api_error_status': 429})['code'] == 'rate_limited'
    assert normalize('claude', 'The usage limit is exceeded')['code'] == 'quota_exhausted'


def test_unknown_provider_fields_are_not_reflected():
    issue = normalize('claude', {'code': 'unknown-secret-provider-code',
                                 'message': 'secret-password', 'details': {'provider_code': {'secret': 'value'}}})
    assert issue['code'] == 'provider_failed'
    assert 'secret' not in json.dumps(issue)


def test_exception_prefers_structured_code_and_never_raw_headers():
    class SDKError(Exception):
        proto_error_code = 'SDK_ERROR_CODE_USAGE_LIMIT_EXCEEDED'
        code = 'resource_exhausted'
        status = 429
        headers = {'Authorization': 'secret-token'}
        details = [{'secret': 'private'}]
    issue = exception('claude', SDKError('secret-body'))
    assert issue['code'] == 'quota_exhausted'
    assert issue['outcome'] == 'unknown' and issue['retryable'] is False
    assert issue['details']['http_status'] == 429
    assert 'secret' not in json.dumps(issue)


def parser(engine):
    events = []
    return Parser(engine, lambda kind, data: events.append((kind, data))), events


def test_codex_native_error_retry_is_observable_without_failing_success():
    parsed, events = parser('codex')
    control = CodexControl(None, {}, parsed.feed, None)
    control.thread_id, control.turn_id = 'thread', 'turn'
    control.event({'method': 'error', 'params': {'threadId': 'thread', 'turnId': 'turn',
        'willRetry': True, 'error': {'message': 'secret-body',
            'codexErrorInfo': {'responseStreamDisconnected': {'httpStatusCode': 502}}}}})
    control.event({'method': 'turn/completed', 'params': {'threadId': 'thread',
                  'turn': {'id': 'turn', 'status': 'completed'}}})
    assert not parsed.failed
    issue = next(data for kind, data in events if kind == 'error')
    assert issue['code'] == 'provider_connection_lost'
    assert issue['provider_retrying'] is True and issue['terminal'] is False
    assert finish(parsed) == ('completed', None, None)
    assert 'secret-body' not in json.dumps(events)


def test_codex_preserves_terminal_safety_error_and_ignores_other_turn():
    parsed, events = parser('codex')
    control = CodexControl(None, {}, parsed.feed, None)
    control.thread_id, control.turn_id = 'thread', 'turn'
    control.event({'method': 'error', 'params': {'threadId': 'thread', 'turnId': 'other',
        'error': {'codexErrorInfo': 'usageLimitExceeded', 'message': 'secret-other'}}})
    assert not events
    control.event({'method': 'turn/completed', 'params': {'threadId': 'thread', 'turn': {
        'id': 'turn', 'status': 'failed', 'error': {
            'codexErrorInfo': 'misalignmentPolicyViolation', 'message': 'secret-body'}}}})
    assert parsed.failed and parsed.terminal == 'failed'
    state, code, issue = finish(parsed)
    assert (state, code) == ('failed', 'safety_blocked')
    assert issue['details']['provider_code'] == 'misalignment_policy_violation'
    assert 'secret-' not in json.dumps(events)


@pytest.mark.parametrize('engine,event', [
    ('codex', {'type': 'turn.interrupted'}),
    ('claude', {'type': 'result', 'subtype': 'success', 'terminal_reason': 'aborted_tools'}),
])
def test_provider_interruptions_preserve_unknown_outcome(engine, event):
    parsed, _ = parser(engine)
    parsed.feed(event)
    state, code, issue = finish(parsed)
    assert state == 'interrupted' and code == 'interrupted'
    assert issue['outcome'] == 'unknown' and issue['retryable'] is False


@pytest.mark.parametrize(('event', 'code'), [
    ({'type': 'result', 'subtype': 'error_max_turns', 'is_error': True}, 'max_turns_exceeded'),
    ({'type': 'result', 'subtype': 'error_max_budget_usd', 'is_error': True}, 'budget_exhausted'),
    ({'type': 'result', 'subtype': 'error_max_structured_output_retries', 'is_error': True}, 'structured_output_failed'),
    ({'type': 'result', 'subtype': 'success', 'stop_reason': 'max_tokens'}, 'output_limit_exceeded'),
    ({'type': 'result', 'subtype': 'success', 'is_error': True, 'api_error_status': 401}, 'authentication_required'),
])
def test_claude_execution_caps_are_not_provider_outages(event, code):
    parsed, _ = parser('claude')
    parsed.feed(event)
    assert finish(parsed)[:2] == ('failed', code)


def test_claude_assistant_error_is_not_fabricated_message():
    parsed, events = parser('claude')
    parsed.feed({'type': 'assistant', 'error': 'billing_error',
                 'message': {'content': [{'type': 'text', 'text': 'secret-body'}]}})
    parsed.feed({'type': 'result', 'subtype': 'success', 'is_error': True})
    assert finish(parsed)[:2] == ('failed', 'billing_required')
    assert not any(kind == 'assistant' for kind, _ in events)
    assert 'secret-body' not in json.dumps(events)


def test_missing_terminal_timeout_and_unresolved_tools_never_imply_safe_retry():
    parsed, _ = parser('codex')
    for kwargs in ({}, {'reason': 'timeout'}, {'exit_code': 1}):
        state, _, issue = finish(parsed, **kwargs)
        assert state == 'interrupted'
        assert issue['outcome'] == 'unknown' and issue['retryable'] is False
    parsed.feed({'type': 'turn.failed', 'error': {'codexErrorInfo': 'usageLimitExceeded'}})
    assert finish(parsed, unresolved=True)[2]['outcome'] == 'unknown'
    assert finish(parsed, reason='user_stop') == ('cancelled', 'user_stop', None)


def test_claude_usage_distinguishes_turn_tokens_from_cumulative_cost():
    parsed, events = parser('claude')
    parsed.feed({'type': 'result', 'subtype': 'success', 'usage': {'input_tokens': 12},
        'total_cost_usd': 1.23, 'modelUsage': {'fixture-model': {'inputTokens': 21, 'contextWindow': 200000}}})
    usage = [data for kind, data in events if kind == 'usage']
    assert usage[0]['scope'] == 'turn' and usage[0]['tokens']['input_tokens'] == 12
    assert 'cost_usd' not in usage[0]
    assert usage[1]['scope'] == 'session' and usage[1]['aggregation'] == 'cumulative'
    assert usage[1]['models']['fixture-model']['contextWindow'] == 200000


def test_codex_pushes_quota_and_keeps_last_usage_distinct_from_total():
    parsed, events = parser('codex')
    control = CodexControl(None, {}, parsed.feed, None)
    control.thread_id, control.turn_id = 'thread', 'turn'
    control.event({'method': 'account/rateLimits/updated', 'params': {'rateLimits': {
        'primary': {'usedPercent': 35, 'resetsAt': 1770000000, 'windowDurationMins': 300}}}})
    control.event({'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread', 'turnId': 'turn',
        'tokenUsage': {'last': {'inputTokens': 123, 'outputTokens': 4},
                      'total': {'inputTokens': 456, 'outputTokens': 10}, 'modelContextWindow': 256000}}})
    assert events[0][0] == 'quota'
    assert events[0][1]['limits']['rateLimits']['primary']['usedPercent'] == 35
    usage = [data for kind, data in events if kind == 'usage']
    assert usage[0]['aggregation'] == 'cumulative' and usage[0]['tokens']['input_tokens'] == 456
    assert usage[1]['aggregation'] == 'latest' and usage[1]['tokens']['input_tokens'] == 123
    assert usage[1]['context_window'] == 256000
