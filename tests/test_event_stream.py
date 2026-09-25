"""Exercise replay, follow and terminal semantics without provider accounts."""
import io
import json
import math
from threading import Event, Thread
import time

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.cli import rpc
from agentbridge.errors import BridgeError
from fixtures.test_proxy_account_fixture import register_verified_proxy_account


@pytest.fixture
def stored_turn(tmp_path):
    with Bridge(tmp_path / 'state') as bridge:
        register_verified_proxy_account(bridge.store, 'fixture', 8317)
        bridge.store.add_session('fixture-session', 'fixture', str(tmp_path), 'fixture-model')
        turn_id, _ = bridge.store.admit('fixture-turn', 'fixture-session', 'request',
                                        RunOptions(), None)
        yield bridge, turn_id


def test_stream_replays_exact_public_events_across_bounded_pages(stored_turn, monkeypatch):
    bridge, turn_id = stored_turn
    for index in range(450):
        bridge.store.emit(turn_id, 'text_delta', {'text': f'chunk-{index}'})
    bridge.store.finish(turn_id, 'completed')
    expected = bridge.turn_events(turn_id)
    limits = []
    original = bridge.store.events

    def observed_events(**options):
        limits.append(options['limit'])
        return original(**options)

    monkeypatch.setattr(bridge.store, 'events', observed_events)
    replayed = list(bridge.turn_events_stream(turn_id, timeout_ms=0))
    assert replayed == expected
    assert [event['seq'] for event in replayed] == sorted({event['seq'] for event in replayed})
    assert limits and max(limits) == 200
    cursor = replayed[201]['seq']
    assert list(bridge.turn_events_stream(turn_id, after_seq=cursor, timeout_ms=0)) == replayed[202:]


def test_stream_delivers_active_events_then_drains_terminal_turn(stored_turn):
    bridge, turn_id = stored_turn
    cursor = bridge.turn_events(turn_id)[-1]['seq']
    allow_finish = Event()

    def produce():
        bridge.store.emit(turn_id, 'tool_call', {'call_id': 'tool-1', 'name': 'fixture-tool'})
        bridge.store.emit(turn_id, 'assistant', {'text': 'An observed answer.'})
        assert allow_finish.wait(3)
        bridge.store.emit(turn_id, 'tool_result', {
            'call_id': 'tool-1', 'name': 'fixture-tool', 'outcome': 'completed'})
        bridge.store.finish(turn_id, 'completed')

    producer = Thread(target=produce)
    producer.start()
    stream = bridge.turn_events_stream(turn_id, after_seq=cursor, timeout_ms=1000)
    try:
        first = next(stream)
        message = next(stream)
        assert first['kind'] == 'tool.started'
        assert message['kind'] == 'message.completed' and message['final'] is True
        assert bridge.store.get('runs', turn_id)['state'] not in {'completed', 'failed'}
        allow_finish.set()
        tail = list(stream)
        assert [event['kind'] for event in tail] == ['tool.completed', 'run.finished']
        assert tail[-1]['final'] is True
        assert [event['seq'] for event in [first, message, *tail]] == sorted(
            event['seq'] for event in [first, message, *tail])
    finally:
        allow_finish.set()
        producer.join(timeout=3)
    assert not producer.is_alive()


def test_stream_idle_timeout_returns_without_changing_active_turn(stored_turn):
    bridge, turn_id = stored_turn
    cursor = bridge.turn_events(turn_id)[-1]['seq']
    started = time.monotonic()
    assert list(bridge.turn_events_stream(turn_id, after_seq=cursor, timeout_ms=120)) == []
    assert 0.10 <= time.monotonic() - started < 1
    assert bridge.store.get('runs', turn_id)['state'] == 'starting'
    assert list(bridge.turn_events_stream(turn_id, timeout_ms=0)) == bridge.turn_events(turn_id)


def test_follow_returns_one_available_page_without_waiting_for_terminal(stored_turn):
    bridge, turn_id = stored_turn
    cursor = bridge.turn_events(turn_id)[-1]['seq']
    for index in range(3):
        bridge.store.emit(turn_id, 'text_delta', {'text': f'part-{index}'})
    started = time.monotonic()
    first = bridge.turn_events(turn_id, after_seq=cursor, limit=2,
                               follow=True, timeout_ms=2000)
    assert time.monotonic() - started < 0.5
    assert [event['kind'] for event in first] == ['message.delta', 'message.delta']
    second = bridge.turn_events(turn_id, after_seq=first[-1]['seq'], limit=2,
                                follow=True, timeout_ms=0)
    assert len(second) == 1 and second[0]['data']['text'] == 'part-2'
    assert bridge.store.get('runs', turn_id)['state'] == 'starting'


def test_follow_waits_for_first_event_then_returns_before_turn_finishes(stored_turn):
    bridge, turn_id = stored_turn
    cursor = bridge.turn_events(turn_id)[-1]['seq']
    allow_finish = Event()

    def produce():
        time.sleep(0.1)
        bridge.store.emit(turn_id, 'tool_call', {'call_id': 'tool-1', 'name': 'fixture-tool'})
        assert allow_finish.wait(5)
        bridge.store.finish(turn_id, 'completed')

    producer = Thread(target=produce)
    producer.start()
    try:
        first = bridge.turn_events(turn_id, after_seq=cursor, follow=True, timeout_ms=1000)
        assert [event['kind'] for event in first] == ['tool.started']
        assert bridge.store.get('runs', turn_id)['state'] == 'starting'
        allow_finish.set()
        finished = bridge.turn_events(turn_id, after_seq=first[-1]['seq'],
                                      follow=True, timeout_ms=3000)
        assert [event['kind'] for event in finished] == ['run.finished']
        assert bridge.turn_events(turn_id, after_seq=finished[-1]['seq'],
                                  follow=True, timeout_ms=1000) == []
    finally:
        allow_finish.set()
        producer.join(timeout=3)
    assert not producer.is_alive()


def test_follow_idle_timeout_is_empty_but_run_events_legacy_still_raises(stored_turn):
    bridge, turn_id = stored_turn
    cursor = bridge.turn_events(turn_id)[-1]['seq']
    started = time.monotonic()
    assert bridge.turn_events(turn_id, after_seq=cursor, follow=True, timeout_ms=120) == []
    assert 0.10 <= time.monotonic() - started < 1
    assert bridge.store.get('runs', turn_id)['state'] == 'starting'
    with pytest.raises(TimeoutError):
        list(bridge.run(turn_id).events(after=cursor, follow=True, timeout=0.05))


def test_rpc_turn_events_uses_the_bounded_long_poll_page(stored_turn):
    bridge, turn_id = stored_turn
    cursor = bridge.turn_events(turn_id)[-1]['seq']
    bridge.store.emit(turn_id, 'compaction', {'observed': True})
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'turns.events', 'params': {
        'turn_id': turn_id, 'after_seq': cursor, 'limit': 1,
        'follow': True, 'timeout_ms': 0}}
    output = io.StringIO()
    rpc(bridge, io.StringIO(json.dumps(request) + '\n'), output)
    response = json.loads(output.getvalue())
    assert len(response['result']) == 1
    assert response['result'][0]['kind'] == 'context.compacted'
    assert bridge.store.get('runs', turn_id)['state'] == 'starting'


@pytest.mark.parametrize('value', [-1, True, math.nan, math.inf, '100'])
def test_stream_rejects_invalid_timeout(stored_turn, value):
    bridge, turn_id = stored_turn
    with pytest.raises(BridgeError) as caught:
        list(bridge.turn_events_stream(turn_id, timeout_ms=value))
    assert caught.value.code == 'invalid_timeout'


@pytest.mark.parametrize('value', [-1, True, math.nan, '1'])
def test_stream_rejects_invalid_cursor(stored_turn, value):
    bridge, turn_id = stored_turn
    with pytest.raises(BridgeError) as caught:
        list(bridge.turn_events_stream(turn_id, after_seq=value))
    assert caught.value.code == 'invalid_pagination'
