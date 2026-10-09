"""Native queue interruption keeps uncertain delivery scoped and never retries it."""

import json

from agentbridge import Bridge
from agentbridge.queueing.connection import request
from test_interactive_inputs import setup_proxy
from test_message_queues import completed, queued, running, until


def test_native_interrupt_lost_ack_keeps_input_and_does_not_repeat_after_client_restart(
    queued, tmp_path,
):
    bridge, instance = queued
    first = bridge.queue_add(instance, 'hold:interrupt-drop-ack')
    turn = running(bridge, first)
    priority = bridge.queue_add(instance, 'priority', idempotency_key='priority')
    later = bridge.queue_add(instance, 'later')
    bridge.queue_dispatch(instance, priority['message_id'], mode='interrupt', expected_turn_id=turn)
    until(lambda: bridge.queue_list(instance)['reason'] == 'native_interrupt_unknown')
    assert bridge.run(turn).status == 'running'
    assert not bridge.run(turn).snapshot['stop_requested']
    other = Bridge(bridge.root)
    try:
        request(other.root, instance, {'action': 'shutdown'}, start=False)
        until(lambda: not other.queue_list(instance)['dispatcher_running'])
        other.queue_dispatch(instance, priority['message_id'], mode='interrupt',
                             expected_turn_id=turn)
        assert other.queue_list(instance)['paused']
        assert other.message_get(priority['message_id'])['turn_id'] is None
        assert len(other.runs()) == 1
        path = bridge.root / 'codex-runtime' / instance / 'fixture-interrupts.json'
        assert len(json.loads(path.read_text())) == 1
        (tmp_path / 'interrupt-drop-ack').touch()
        completed(other, first)
        assert other.message_get(priority['message_id'])['turn_id'] is None
        other.queue_resume(instance)
        completed(other, later)
        assert [row['prompt'] for row in other.runs()] == [
            'hold:interrupt-drop-ack', 'priority', 'later']
        assert len(json.loads(path.read_text())) == 1
        actions = [event['data'].get('action') for event in other.instance_events(instance)
                   if event['kind'] == 'queue.changed']
        assert actions.count('native_interrupt_sent') == 1
        assert actions.count('native_interrupt_unknown') == 1
    finally:
        other.close()
