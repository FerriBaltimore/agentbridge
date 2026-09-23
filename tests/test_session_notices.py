import json
from pathlib import Path

from agentbridge import Bridge, RunOptions
from agentbridge.codex_control import CodexControl
from agentbridge.execution_outcome import finish
from agentbridge.protocols import Parser
from fixtures.test_proxy_account_fixture import register_verified_proxy_account


def test_claude_native_retry_and_routing_are_not_host_policy():
    events = []
    parsed = Parser('claude', lambda kind, data: events.append((kind, data)))
    parsed.feed({'type': 'system', 'subtype': 'api_retry', 'attempt': 1, 'max_retries': 3,
        'retry_delay_ms': 2000, 'error_status': 429, 'error': 'rate_limit', 'content': 'secret-body'})
    parsed.feed({'type': 'system', 'subtype': 'model_refusal_fallback', 'direction': 'retry',
        'original_model': 'primary', 'fallback_model': 'fallback', 'scope': 'local',
        'content': 'secret-body', 'api_refusal_explanation': 'secret-explanation',
        'retracted_message_uuids': ['retracted-1']})
    assert events[0][0] == 'retry'
    assert events[0][1]['error']['code'] == 'rate_limited'
    assert events[0][1]['retry_delay_ms'] == 2000
    changed = events[1][1]
    assert changed['scope'] == 'local' and changed['from_model'] == 'primary'
    assert changed['to_model'] == 'fallback' and changed['host_initiated'] is False
    assert 'secret-' not in json.dumps(events)
    parsed.feed({'type': 'result', 'subtype': 'success'})
    assert finish(parsed) == ('completed', None, None)


def test_no_fallback_refusal_cannot_be_recorded_as_success():
    parsed = Parser('claude', lambda *_: None)
    parsed.feed({'type': 'system', 'subtype': 'model_refusal_no_fallback', 'content': 'secret-body'})
    parsed.feed({'type': 'result', 'subtype': 'success'})
    assert finish(parsed)[:2] == ('failed', 'safety_blocked')


def test_codex_native_reroute_preserves_observed_models():
    events = []
    parsed = Parser('codex', lambda kind, data: events.append((kind, data)))
    control = CodexControl(None, {}, parsed.feed, None)
    control.thread_id, control.turn_id = 'thread', 'turn'
    control.event({'method': 'model/rerouted', 'params': {'threadId': 'thread', 'turnId': 'turn',
        'fromModel': 'primary', 'toModel': 'fallback', 'reason': 'highRiskCyberActivity'}})
    assert events[0][0] == 'model_changed'
    assert events[0][1]['reason'] == 'safety_blocked'
    assert events[0][1]['scope'] == 'turn' and events[0][1]['host_initiated'] is False


def stored(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    register_verified_proxy_account(bridge.store, 'fixture', 19321)
    bridge.store.add_session('fixture-session', 'fixture', str(tmp_path), 'fixture-model')
    session = bridge.get_session('fixture-session')
    turn, _ = bridge.store.admit('fixture-turn', session['id'], 'fixture', RunOptions(), None)
    parsed = Parser('claude', lambda kind, data: bridge.store.emit(turn, kind, data))
    return bridge, session['id'], turn, parsed


def test_retraction_changes_projection_but_preserves_evidence(tmp_path):
    bridge, instance, turn, parsed = stored(tmp_path)
    parsed.feed({'type': 'stream_event', 'event': {'type': 'content_block_delta',
                 'delta': {'type': 'text_delta', 'text': 'old response'}}})
    parsed.feed({'type': 'assistant', 'uuid': 'message-old',
                 'message': {'content': [{'type': 'text', 'text': 'old response'}]}})
    parsed.feed({'type': 'system', 'subtype': 'model_refusal_fallback', 'direction': 'retry',
        'original_model': 'primary', 'fallback_model': 'fallback', 'retracted_message_uuids': ['message-old']})
    assert bridge.run(turn).text == ''  # No resurrection through the earlier deltas.
    row = bridge.messages(instance, role='assistant')[0]
    assert row['content'] == '' and row['retracted'] is True
    parsed.feed({'type': 'assistant', 'uuid': 'message-new',
                 'message': {'content': [{'type': 'text', 'text': 'replacement'}]}})
    bridge.store.finish(turn, 'completed')
    assert bridge.run(turn).text == 'replacement'
    row = bridge.messages(instance, role='assistant')[0]
    assert row['content'] == 'replacement' and row['incomplete'] is False
    assert any(event.data.get('text') == 'old response' for event in bridge.run(turn).events())
    context = bridge.export_context(instance)
    assert 'old response' not in context.text and 'replacement' in context.text
    payload = json.loads(context.text.split('\n', 1)[1])
    assert len(payload['retracted_message_seqs']) == 1
    assert payload['retracted_message_seqs'][0] in context.omitted
    assert 'old response' in Path(context.archive_path).read_text()


def test_supersedes_and_aborted_partial_are_visible_without_requiring_notice(tmp_path):
    bridge, instance, turn, parsed = stored(tmp_path)
    parsed.feed({'type': 'assistant', 'uuid': 'message-old',
                 'message': {'content': [{'type': 'text', 'text': 'old response'}]}})
    parsed.feed({'type': 'assistant', 'uuid': 'message-new', 'supersedes': ['message-old'], 'aborted': True,
                 'message': {'content': [{'type': 'text', 'text': 'partial replac'}]}})
    bridge.store.finish(turn, 'interrupted', 'interrupted')
    assert bridge.run(turn).text == 'partial replac'
    row = bridge.messages(instance, role='assistant')[0]
    assert row['incomplete'] is True and row['retracted'] is True
    assert row['retracted_provider_message_ids'] == ['message-old']


def test_completed_deltas_are_complete_and_unknown_retraction_is_noop(tmp_path):
    bridge, _, turn, _ = stored(tmp_path)
    bridge.store.emit(turn, 'text_delta', {'text': 'kept'})
    bridge.store.emit(turn, 'model_changed', {'retracted_provider_message_ids': ['unknown-id']})
    bridge.store.finish(turn, 'completed')
    assert bridge.run(turn).message['text'] == 'kept'
    assert bridge.run(turn).message['incomplete'] is False
