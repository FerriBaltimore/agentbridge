from agentbridge import Account, Bridge, RunOptions
from agentbridge.event_contract import public_event
from agentbridge.models import Event
from agentbridge.protocols import Parser
from agentbridge.quota_windows import project


def test_claude_stream_fraction_is_not_oauth_percent():
    event = {'source': 'claude_stream', 'limits': {
        'rateLimitType': 'seven_day', 'utilization': 1, 'resetsAt': 2000, 'status': 'rejected',
        'unifiedWindows': {'five_hour': {'utilization': .95, 'resetsAt': 1500},
                           'seven_day': {'utilization': 1.1, 'resetsAt': 2000}}}, 'observed_at': 1000}
    rows = project('claude', event, now=1001)['windows']
    assert rows[0]['used_percent'] == 95 and rows[0]['reset_after_seconds'] == 499
    assert round(rows[1]['used_percent']) == 110 and rows[1]['status'] == 'rejected'
    assert rows[1]['limit_reached'] and rows[1]['remaining_percent'] == 0
    oauth = project('claude', {'five_hour': {'utilization': .95}, 'observed_at': 1000}, now=1001)
    assert oauth['windows'][0]['used_percent'] == .95


def test_claude_sparse_stream_keeps_unknown_usage_and_rejection():
    value = project('claude', {'rateLimitType': 'seven_day', 'status': 'rejected', 'resetsAt': 2000}, now=1001)
    row = value['windows'][0]
    assert row['used_percent'] is None and row['remaining_percent'] is None
    assert row['limit_reached'] is True and row['reset_after_seconds'] == 999


def test_public_quota_event_normalizes_nested_native_payload():
    events = []
    Parser('claude', lambda kind, data: events.append((kind, data))).feed({
        'type': 'rate_limit_event', 'rate_limit_info': {'rateLimitType': 'five_hour', 'utilization': .5}})
    kind, data = events[0]
    event = Event(1, 'turn', 'session', kind, 1000, data)
    public = public_event(event, 'claude')
    assert public['kind'] == 'quota.observed'
    assert public['data']['windows'][0]['used_percent'] == 50


def test_safety_error_is_identical_in_turn_and_final_event(tmp_path):
    bridge = Bridge(tmp_path)
    bridge.register(Account('fixture', 'codex', home=str(tmp_path)))
    instance = bridge.instance_create(account_ref='fixture', workspace_path=str(tmp_path))
    turn, _ = bridge.store.admit('fixture-turn', instance['id'], 'test', RunOptions(), 'fixture')
    parser = Parser('codex', lambda kind, data: bridge.store.emit(turn, kind, data))
    parser.feed({'type': 'turn.failed', 'error': {'message':
        'This request was blocked by our safety systems. Reason: Potentially unintended activity. PRIVATE BODY'}})
    bridge.store.finish(turn, 'failed', 'safety_blocked')
    value = bridge.turn(turn, include_error=True)
    final = bridge.turn_events(turn)[-1]
    assert value['outcome'] == 'failed'
    assert value['error_detail']['code'] == 'safety_blocked'
    assert value['error_detail']['retryable'] is False
    assert final['data']['error_detail'] == value['error_detail']
    assert 'PRIVATE BODY' not in str(bridge.turn_events(turn))
