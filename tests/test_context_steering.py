"""Promotion preserves selected input boundaries rather than replacing active context."""

from uuid import uuid4

import pytest

from agentbridge.errors import BridgeError
from test_interactive_inputs import context_package as original_package, setup_proxy
from agentbridge.context_package import digest
from test_message_queues import queued, running, until



def context_package(*, tools=()):
    value = original_package(tools=tools)
    value['instructions'] = value['instructions'][:1]
    value['selection_hash'] = digest({'context_refs': ['fixture:1'],
        'instructions': [{**item, 'assets': [{'path': asset['path'], 'digest': asset['digest']}
            for asset in item['assets']]} for item in value['instructions']],
        'exclusions': [], 'tools': list(tools)})
    return value


def test_identical_selected_context_can_steer_but_changed_context_stays_pending(queued):
    bridge, instance = queued
    package = context_package()
    first = bridge.queue_add(instance, 'hold:never', context_package=package)
    turn = running(bridge, first)
    # A message without the package may add plain live input while retaining active context.
    different = context_package()
    different['exclusions'] = ['excluded-rule']
    different['selection_hash'] = digest({
        'context_refs': ['fixture:1'],
        'instructions': [{**item, 'assets': [{'path': asset['path'], 'digest': asset['digest']}
            for asset in item['assets']]} for item in different['instructions']],
        'exclusions': different['exclusions'], 'tools': []})
    denied = bridge.queue_add(instance, 'incompatible', context_package=different)
    with pytest.raises(BridgeError) as failure:
        bridge.queue_dispatch(instance, denied['message_id'], mode='steer', expected_turn_id=turn)
    assert failure.value.code == 'steering_context_unsupported'
    assert bridge.message_get(denied['message_id'])['state'] == 'queued'
    finish = bridge.queue_add(instance, 'finish', context_package=package)
    bridge.queue_dispatch(instance, finish['message_id'], mode='steer', expected_turn_id=turn)
    until(lambda: bridge.message_get(finish['message_id'])['state'] == 'delivered')
    assert len([row for row in bridge.runs() if row['id'] == turn]) == 1
    assert bridge.run(turn).wait(10)['state'] == 'completed'


def test_steering_requires_the_same_selected_mcp_endpoint(queued, tmp_path):
    bridge, instance = queued
    package = context_package(tools=({'name': 'fixture-tool'},))
    descriptor = {'version': 1, 'socket_path': str(tmp_path / 'tools.sock'),
                  'operation_id': str(uuid4()), 'capability': 'FIXTURE_MCP_CAPABILITY_123456'}
    first = bridge.queue_add(instance, 'hold:never', context_package=package, mcp=descriptor)
    turn = running(bridge, first)
    other = bridge.queue_add(instance, 'other', context_package=package,
        mcp={**descriptor, 'operation_id': str(uuid4()), 'socket_path': str(tmp_path / 'other.sock')})
    with pytest.raises(BridgeError) as failure:
        bridge.queue_dispatch(instance, other['message_id'], mode='steer', expected_turn_id=turn)
    assert failure.value.code == 'steering_context_unsupported'
    finish = bridge.queue_add(instance, 'finish', context_package=package,
                              mcp={**descriptor, 'operation_id': str(uuid4())})
    bridge.queue_dispatch(instance, finish['message_id'], mode='steer', expected_turn_id=turn)
    until(lambda: bridge.message_get(finish['message_id'])['state'] == 'delivered')
    assert bridge.run(turn).wait(10)['state'] == 'completed'
