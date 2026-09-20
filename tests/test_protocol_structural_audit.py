"""Pinned native state contracts reject future or malformed success assumptions."""
import json
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

from agentbridge.codex_control import CodexControl
from agentbridge.claude_control import execute as claude_execute, initialize
from agentbridge.cursor_worker import execute as cursor_execute
from agentbridge.errors import BridgeError
from agentbridge.execution_outcome import finish
from agentbridge.protocols import Parser
from agentbridge.provider_channel import ProviderChannel
from agentbridge.provider_errors import normalize


def parser(engine):
    events = []
    return Parser(engine, lambda kind, data: events.append((kind, data))), events


def control(parsed):
    value = CodexControl(None, {}, parsed.feed, None)
    value.thread_id, value.turn_id = 'thread', 'turn'
    return value


@pytest.mark.parametrize('event', [
    'malformed provider event',
    {'type': 'result'},
    {'type': 'result', 'subtype': 'future_success'},
    {'type': 'result', 'subtype': 'success', 'is_error': 'false'},
    {'type': 'result', 'subtype': 'success', 'terminal_reason': 'future_finished'},
    {'type': 'result', 'subtype': 'success', 'terminal_reason': 'tool_deferred'},
    {'type': 'result', 'subtype': 'success', 'stop_reason': 'future_finished'},
    {'type': ['result']},
    {'type': 'stream_event', 'event': ['malformed']},
])
def test_claude_unknown_result_shapes_cannot_claim_completion(event):
    parsed, events = parser('claude')
    parsed.feed(event)
    state, code, issue = finish(parsed)
    assert (state, code) == ('interrupted', 'provider_protocol_error')
    assert issue['outcome'] == 'unknown' and issue['retryable'] is False
    assert any(kind == 'gap' for kind, _ in events)


def test_older_claude_refusal_without_system_notice_is_a_safety_failure():
    parsed, _ = parser('claude')
    parsed.feed({'type': 'result', 'subtype': 'success', 'stop_reason': 'refusal'})
    assert finish(parsed)[:2] == ('failed', 'safety_blocked')


@pytest.mark.parametrize('terminal_reason,code', [
    ('prompt_too_long', 'context_window_exceeded'), ('max_turns', 'max_turns_exceeded'),
    ('budget_exhausted', 'budget_exhausted'),
    ('structured_output_retry_exhausted', 'structured_output_failed'),
])
def test_claude_terminal_reason_overrides_nominal_success(terminal_reason, code):
    parsed, _ = parser('claude')
    parsed.feed({'type': 'result', 'subtype': 'success', 'terminal_reason': terminal_reason})
    assert finish(parsed)[:2] == ('failed', code)


def test_claude_sparse_task_patch_preserves_observed_status():
    parsed, events = parser('claude')
    parsed.feed({'type': 'system', 'subtype': 'task_notification', 'task_id': 'child', 'status': 'completed'})
    parsed.feed({'type': 'system', 'subtype': 'task_updated', 'task_id': 'child',
                 'patch': {'description': 'updated label', 'is_backgrounded': True}})
    assert parsed.tasks['child'] == 'completed'
    assert events[-1][1]['background'] is True
    assert parsed.end() is False


@pytest.mark.parametrize('status', ['completed', 'success', 'ok', 'succeeded', 'future', None, {}])
def test_cursor_accepts_only_the_published_terminal_state_contract(status):
    parsed, events = parser('cursor')
    parsed.feed({'type': 'bridge_result', 'status': status})
    assert finish(parsed)[:2] == ('interrupted', 'provider_protocol_error')
    assert any(kind == 'gap' for kind, _ in events)


def test_cursor_finished_is_the_native_success_state():
    parsed, _ = parser('cursor')
    parsed.feed({'type': 'bridge_result', 'status': 'finished'})
    assert finish(parsed) == ('completed', None, None)


def test_codex_unpublished_terminal_state_is_unknown_not_definite_failure():
    parsed, _ = parser('codex')
    native = control(parsed)
    native.event({'method': 'turn/completed', 'params': {
        'threadId': 'thread', 'turn': {'id': 'turn', 'status': 'future_suspended'}}})
    state, code, issue = finish(parsed)
    assert (state, code) == ('interrupted', 'provider_protocol_error')
    assert issue['outcome'] == 'unknown' and native.done


def test_interrupted_native_turn_preserves_its_structured_cause():
    parsed, _ = parser('codex')
    parsed.feed({'type': 'turn.interrupted', 'error': {'codexErrorInfo': 'cyberPolicy'}})
    state, code, issue = finish(parsed)
    assert (state, code) == ('interrupted', 'safety_blocked')
    assert issue['outcome'] == 'unknown' and issue['retryable'] is False


def test_codex_missing_tool_status_does_not_invent_success():
    parsed, events = parser('codex')
    parsed.feed({'type': 'item.completed', 'item': {'id': 'tool', 'type': 'command_execution'}})
    assert events[-1][0] == 'tool_result'
    assert events[-1][1]['outcome'] == 'unknown'


def test_codex_native_declined_tool_is_not_unknown_or_completed():
    parsed, events = parser('codex')
    parsed.feed({'type': 'item.completed', 'item': {
        'id': 'tool', 'type': 'command_execution', 'status': 'declined'}})
    assert events[-1][1]['outcome'] == 'failed'


def test_codex_mcp_error_and_dynamic_result_survive_native_adapter():
    parsed, events = parser('codex')
    native = control(parsed)
    native.event({'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn',
        'item': {'id': 'mcp', 'type': 'mcpToolCall', 'status': 'completed', 'server': 'fixture',
                 'tool': 'fixture', 'arguments': {}, 'error': {'message': 'secret-provider-body'}}}})
    native.event({'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn',
        'item': {'id': 'dynamic', 'type': 'dynamicToolCall', 'status': 'completed',
                 'tool': 'fixture', 'arguments': {}, 'success': False, 'contentItems': []}}})
    results = [data for kind, data in events if kind == 'tool_result']
    assert len(results) == 2 and all(item['outcome'] == 'failed' for item in results)
    assert results[0]['output']['error']['code'] == 'provider_failed'
    assert 'secret-provider-body' not in json.dumps(events)


def test_codex_tool_failure_is_observed_without_declaring_the_whole_turn_failed():
    observed, events = [], []
    def observe(issue):
        observed.append(issue)
        return issue
    parsed = Parser('codex', lambda kind, data: events.append((kind, data)), error_handler=observe)
    parsed.feed({'type': 'item.completed', 'item': {'id': 'tool', 'type': 'mcp_tool_call',
                 'error': {'message': 'unknown upstream service failure'}}})
    parsed.feed({'type': 'turn.completed'})
    assert len(observed) == 1 and observed[0]['code'] == 'provider_failed'
    assert events[1][1]['outcome'] == 'failed'
    assert finish(parsed) == ('completed', None, None)


@pytest.mark.parametrize('status', ['completed', 'errored', 'interrupted', 'shutdown', 'notFound'])
def test_codex_collaboration_statuses_are_observed_without_an_invented_pending_task(status):
    parsed, events = parser('codex')
    control(parsed).event({'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn',
        'item': {'id': 'spawn', 'type': 'collabAgentToolCall', 'status': 'completed',
                 'tool': 'wait', 'senderThreadId': 'parent', 'receiverThreadIds': ['child'],
                 'agentsStates': {'child': {'status': status, 'message': 'private explanation'}}}}})
    assert events[-1] == ('subagent', {'agent_id': 'child', 'status': status,
                                      'parent_id': 'parent', 'tool': 'wait'})
    assert parsed.end() is False
    assert 'private explanation' not in json.dumps(events)


def test_codex_future_items_and_notices_remain_explicit_gaps():
    parsed, events = parser('codex')
    native = control(parsed)
    native.event({'method': 'item/completed', 'params': {'threadId': 'thread',
        'item': {'id': 'future', 'type': 'futureItem', 'privateContent': 'private-body'}}})
    native.event({'method': 'future/event', 'params': {'privateContent': 'private-body'}})
    assert [kind for kind, _ in events] == ['gap', 'gap']
    assert 'private-body' not in json.dumps(events)


@pytest.mark.parametrize('params', [[], ['invalid'], 'private-body', 42])
def test_codex_malformed_params_are_structured_protocol_errors(params):
    native = control(parser('codex')[0])
    with pytest.raises(BridgeError) as error:
        native.event({'method': 'turn/completed', 'params': params})
    assert error.value.code == 'provider_protocol_error'
    assert error.value.outcome == 'unknown'


def test_codex_invalid_counters_do_not_become_zero_observations():
    parsed, events = parser('codex')
    native = control(parsed)
    native.event({'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread',
        'tokenUsage': {'total': {'inputTokens': -2, 'outputTokens': False},
                       'last': {'inputTokens': 4}, 'modelContextWindow': True}}})
    usage = [data for kind, data in events if kind == 'usage']
    assert len(usage) == 1 and usage[0]['tokens'] == {'input_tokens': 4}
    assert usage[0]['context_window'] is None


@pytest.mark.parametrize('status,code', [(401, 'authentication_required'), (429, 'rate_limited'),
                                      (503, 'provider_unavailable')])
def test_structured_http_status_has_priority_over_generic_body_heuristics(status, code):
    issue = normalize('codex', {'status_code': status, 'message': 'usage limit reached'})
    assert issue['code'] == code and issue['details']['detection'] == 'http_status'


def test_generic_limit_text_and_ambiguous_native_enum_are_not_invented_quota():
    assert normalize('claude', 'Attachment limit reached')['code'] == 'provider_failed'
    assert normalize('claude', 'This is not your usage limit')['code'] == 'provider_failed'
    assert normalize('claude', 'API rate limit, not your usage limit')['code'] == 'rate_limited'
    issue = normalize('codex', {'codexErrorInfo': {'usageLimitExceeded': {}, 'futureFailure': {}}})
    assert issue['code'] == 'provider_failed'


def test_closed_native_pipe_has_unknown_outcome_and_launch_failure_does_not(tmp_path):
    with ProviderChannel([sys.executable, '-c', 'pass'], cwd=tmp_path, env=os.environ.copy()) as channel:
        with pytest.raises(BridgeError) as error:
            channel.receive(2)
        assert error.value.code == 'provider_connection_lost'
        assert error.value.outcome == 'unknown'
    with pytest.raises(BridgeError) as error:
        ProviderChannel([str(tmp_path / 'missing-program')], cwd=tmp_path, env={})
    assert error.value.code == 'provider_unavailable'
    assert error.value.phase == 'launch' and error.value.outcome == 'not_started'


def test_claude_malformed_initialization_cannot_start_a_turn():
    channel = SimpleNamespace(send=lambda value: None, receive=lambda timeout: {
        'type': 'control_response', 'response': {'request_id': 'initialize',
        'subtype': 'success', 'response': ['invalid']}})
    with pytest.raises(BridgeError) as error:
        initialize(channel)
    assert error.value.code == 'provider_protocol_error'
    assert error.value.outcome == 'not_started'


@pytest.mark.parametrize('native_request', [
    {'subtype': 'can_use_tool', 'tool_name': 'Read', 'input': []},
    {'subtype': 'can_use_tool', 'tool_name': None, 'input': {}},
])
def test_claude_malformed_permission_never_reaches_approval(native_request):
    values = iter([
        {'type': 'control_response', 'response': {'request_id': 'initialize',
            'subtype': 'success', 'response': {}}},
        {'type': 'control_request', 'request_id': 'permission', 'request': native_request},
    ])
    approvals = []
    channel = SimpleNamespace(send=lambda value: None, receive=lambda timeout: next(values))
    with pytest.raises(BridgeError) as error:
        claude_execute(channel, {'prompt': 'fixture', 'options': {'timeout': 1}},
                       lambda event: None, lambda *args: approvals.append(args))
    assert error.value.code == 'provider_protocol_error' and approvals == []


@pytest.mark.parametrize('run_usage', [None, {'input_tokens': 7, 'output_tokens': 3}])
def test_cursor_uses_reported_run_usage_and_never_fills_absence_with_zero(tmp_path, monkeypatch, run_usage):
    fixture = runpy.run_path(str(Path(__file__).parent / 'fixtures' / 'test_cursor_sdk_provider.py'))
    sdk, events = fixture['sdk'], []
    monkeypatch.setenv('FIXTURE_CURSOR_KEY', 'fixture-key-never-real')
    monkeypatch.setattr(sdk.Agent, 'wait', lambda self: SimpleNamespace(status='finished', usage=run_usage))
    cursor_execute({'cwd': str(tmp_path), 'model': 'fixture', 'key_env': 'FIXTURE_CURSOR_KEY',
                    'prompt': 'fixture'}, events.append, sdk=sdk)
    usage = [item for item in events if item['type'] == 'bridge_usage']
    assert usage == ([] if run_usage is None else [{'type': 'bridge_usage', 'scope': 'turn', 'usage': run_usage}])
    assert events[-1] == {'type': 'bridge_result', 'status': 'finished'}
