"""Conversation queue subprocess tests use isolated fixture accounts only."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time

import pytest

from agentbridge import Bridge
from agentbridge.errors import BridgeError
from agentbridge.models import TERMINAL
from agentbridge.queueing.connection import request
from agentbridge.rpc import dispatch
from test_interactive_inputs import setup_proxy


FIXTURE = Path(__file__).parent / 'fixtures/test_queue_provider.py'


def until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.025)
    raise AssertionError('The queue did not reach the expected state.')


@pytest.fixture
def queued(setup_proxy):
    bridge, instance = setup_proxy(native_command=('/usr/bin/python3', '-c', FIXTURE.read_text()))
    yield bridge, instance
    bridge.queue_pause(instance)
    request(bridge.root, instance, {'action': 'shutdown'}, start=False)
    for row in bridge.runs():
        if row['state'] not in TERMINAL:
            bridge.turn_stop(row['id'], wait=True)


def running(bridge, message):
    value = until(lambda: bridge.message_get(message['message_id'])['turn_id'])
    until(lambda: any(event['kind'] == 'message.delta' for event in bridge.turn_events(value)))
    return value


def completed(bridge, message):
    message_id = message['message_id']
    until(lambda: bridge.message_get(message_id)['state'] == 'completed')
    return bridge.message_get(message_id)


def native_history(bridge, instance):
    path = bridge.root / 'codex-runtime' / instance / 'fixture-queue-native.json'
    return json.loads(path.read_text())


def test_fifo_survives_client_restart_and_preserves_message_identity(queued, tmp_path):
    bridge, instance = queued
    first = bridge.queue_add(instance, 'hold:release')
    first_turn = running(bridge, first)
    second = bridge.queue_add(instance, 'second', idempotency_key='second-key')
    third = bridge.queue_add(instance, 'third')
    assert second['turn_id'] is None and second['state'] == 'queued'
    other = Bridge(bridge.root)
    assert [item['message_id'] for item in other.queue_list(instance)['items']] == [
        second['message_id'], third['message_id']]
    assert other.queue_add(instance, 'second', idempotency_key='second-key')['replayed']
    assert len(bridge.runs()) == 1
    (tmp_path / 'release').touch()
    completed(other, third)
    assert [row['message_id'] for row in bridge.runs()] == [
        first['message_id'], second['message_id'], third['message_id']]
    assert bridge.run(first_turn).status == 'completed'
    assert other.queue_list(instance)['total'] == 0
    assert native_history(bridge, instance) == {
        'methods': ['thread/start', 'thread/resume', 'thread/resume'],
        'prompts': ['hold:release', 'second', 'third'],
    }
    events = bridge.instance_events(instance)
    assert any(event['kind'] == 'queue.changed' and event['turn_id'] is None for event in events)
    assert any(event['message_id'] == second['message_id'] and event['kind'] == 'message.created'
               for event in events)


def test_insert_reorder_delete_and_stale_version(queued):
    bridge, instance = queued
    bridge.queue_pause(instance)
    one = bridge.queue_add(instance, 'one')
    two = bridge.queue_add(instance, 'two')
    three = bridge.queue_add(instance, 'three', position=1)
    snapshot = bridge.queue_list(instance)
    assert [item['content'] for item in snapshot['items']] == ['one', 'three', 'two']
    moved = bridge.queue_move(instance, two['message_id'], 0, expected_version=snapshot['version'])
    assert [item['content'] for item in moved['items']] == ['two', 'one', 'three']
    with pytest.raises(BridgeError) as error:
        bridge.queue_delete(instance, one['message_id'], expected_version=snapshot['version'])
    assert error.value.code == 'version_conflict'
    bridge.queue_delete(instance, three['message_id'], expected_version=moved['version'])
    assert bridge.queue_delete(instance, three['message_id'])['replayed']
    bridge.queue_resume(instance)
    completed(bridge, one)
    assert [row['prompt'] for row in bridge.runs()] == ['two', 'one']
    assert bridge.message_get(three['message_id'])['state'] == 'cancelled'


def test_competing_queue_edits_are_atomic(queued):
    bridge, instance = queued
    bridge.queue_pause(instance)
    one = bridge.queue_add(instance, 'one')
    bridge.queue_add(instance, 'two')
    version = bridge.queue_list(instance)['version']

    def move(position):
        try:
            Bridge(bridge.root).queue_move(instance, one['message_id'], position, expected_version=version)
            return 'moved'
        except BridgeError as error:
            return error.code

    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(move, (0, 1)))
    assert sorted(results) == ['moved', 'version_conflict']
    assert [item['position'] for item in bridge.queue_list(instance)['items']] == [0, 1]


def test_simultaneous_idempotent_add_only_schedules_once(queued):
    bridge, instance = queued
    bridge.queue_pause(instance)

    def add(_):
        return Bridge(bridge.root).queue_add(instance, 'once', idempotency_key='once')

    with ThreadPoolExecutor(4) as executor:
        values = list(executor.map(add, range(4)))
    assert len({item['message_id'] for item in values}) == 1
    assert sum(not item['replayed'] for item in values) == 1
    assert bridge.queue_list(instance)['total'] == 1
    with pytest.raises(BridgeError) as error:
        bridge.queue_add(instance, 'changed', idempotency_key='once')
    assert error.value.code == 'idempotency_conflict'
    bridge.queue_resume(instance)
    completed(bridge, values[0])
    assert len(bridge.runs()) == 1


def test_queued_message_can_steer_active_turn_without_starting_another(queued):
    bridge, instance = queued
    first = bridge.queue_add(instance, 'hold:never')
    turn = running(bridge, first)
    later = bridge.queue_add(instance, 'later')
    immediate = bridge.queue_add(instance, 'finish')
    result = bridge.queue_dispatch(instance, immediate['message_id'], mode='steer', expected_turn_id=turn)
    assert result['turn_id'] == turn
    until(lambda: bridge.message_get(immediate['message_id'])['state'] == 'delivered')
    completed(bridge, later)
    assert len(bridge.runs()) == 2
    assert bridge.run(turn).text.endswith('|steer=finish')
    assert sum(event['kind'] == 'message.created' and event['message_id'] == immediate['message_id']
               for event in bridge.turn_events(turn)) == 1
    bridge.queue_dispatch(instance, immediate['message_id'], mode='steer', expected_turn_id=turn)
    assert len(bridge.runs()) == 2


def test_interrupt_stops_exact_turn_and_promotes_requested_message(queued):
    bridge, instance = queued
    first = bridge.queue_add(instance, 'hold:never')
    turn = running(bridge, first)
    later = bridge.queue_add(instance, 'later')
    priority = bridge.queue_add(instance, 'priority')
    with pytest.raises(BridgeError) as error:
        bridge.queue_dispatch(instance, priority['message_id'], mode='interrupt', expected_turn_id='wrong')
    assert error.value.code == 'turn_conflict'
    assert not bridge.run(turn).snapshot['stop_requested']
    bridge.queue_dispatch(instance, priority['message_id'], mode='interrupt', expected_turn_id=turn)
    completed(bridge, later)
    assert bridge.run(turn).status == 'cancelled'
    assert [row['prompt'] for row in bridge.runs()] == ['hold:never', 'priority', 'later']
    assert native_history(bridge, instance) == {
        'methods': ['thread/start', 'thread/resume', 'thread/resume'],
        'prompts': ['hold:never', 'priority', 'later'],
    }


def test_stop_pauses_pending_work_until_explicit_resume(queued):
    bridge, instance = queued
    first = bridge.queue_add(instance, 'hold:never')
    turn = running(bridge, first)
    second = bridge.queue_add(instance, 'second')
    bridge.turn_stop(turn, wait=True)
    assert bridge.queue_list(instance)['paused']
    assert bridge.message_get(second['message_id'])['turn_id'] is None
    bridge.queue_resume(instance)
    completed(bridge, second)
    assert native_history(bridge, instance) == {
        'methods': ['thread/start', 'thread/resume'], 'prompts': ['hold:never', 'second']}


def test_native_rejection_and_lost_ack_are_not_replayed(queued):
    bridge, instance = queued
    first = bridge.queue_add(instance, 'hold:never')
    turn = running(bridge, first)
    rejected = bridge.message_create(instance, 'reject', delivery='steer')
    until(lambda: bridge.message_get(rejected['message_id'])['state'] == 'rejected')
    assert bridge.run(turn).status == 'running'
    later = bridge.queue_add(instance, 'later')
    lost = bridge.message_create(instance, 'drop', delivery='steer', idempotency_key='drop')
    until(lambda: bridge.message_get(lost['message_id'])['state'] == 'unknown')
    assert bridge.queue_list(instance)['paused']
    replay = bridge.message_create(instance, 'drop', delivery='steer', idempotency_key='drop')
    assert replay['replayed'] and replay['state'] == 'unknown'
    assert len(bridge.runs()) == 1
    assert bridge.message_get(later['message_id'])['turn_id'] is None


def test_rpc_queue_controls_and_credentials_redaction(queued):
    bridge, instance = queued
    dispatch(bridge, 'queues.pause', {'instance_id': instance})
    item = dispatch(bridge, 'messages.create', {'instance_id': instance,
        'content': 'fixture-client-key and fixture-management-key', 'delivery': 'queue'})
    listing = dispatch(bridge, 'queues.list', {'instance_id': instance})
    assert listing['items'][0]['content'] == '[redacted] and [redacted]'
    assert dispatch(bridge, 'messages.get', {'message_id': item['message_id']})['state'] == 'queued'
    saved = bridge.store.path.read_bytes()
    assert b'fixture-client-key' not in saved and b'fixture-management-key' not in saved
    moved = dispatch(bridge, 'queues.move', {'instance_id': instance,
        'message_id': item['message_id'], 'position': 0, 'expected_version': listing['version']})
    assert moved['total'] == 1
    dispatch(bridge, 'queues.delete', {'instance_id': instance, 'message_id': item['message_id']})
    assert dispatch(bridge, 'queues.resume', {'instance_id': instance})['total'] == 0
    assert not bridge.runs()


def test_new_live_input_inherits_settings_and_replays_after_completion(queued):
    bridge, instance = queued
    first = bridge.queue_add(instance, 'hold:never', permission_mode='default',
                             sandbox_mode='workspace-write', effort='high')
    turn = running(bridge, first)
    with pytest.raises(BridgeError) as error:
        bridge.message_create(instance, 'restrict this turn', delivery='steer', sandbox_mode='read-only')
    assert error.value.code == 'steering_options_conflict'
    assert bridge.queue_list(instance)['total'] == 0
    addition = bridge.message_create(instance, 'finish', delivery='steer', idempotency_key='live-settings')
    completed(bridge, first)
    assert bridge.message_get(addition['message_id'])['state'] == 'delivered'
    replay = bridge.message_create(instance, 'finish', delivery='steer', idempotency_key='live-settings')
    assert replay['replayed'] and replay['turn_id'] == turn
    assert len(bridge.runs()) == 1
