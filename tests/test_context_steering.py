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


def test_explicit_promotion_keeps_active_context_and_original_queued_identity(queued):
    bridge, instance = queued
    package = context_package()
    first = bridge.queue_add(instance, 'hold:never', context_package=package)
    turn = running(bridge, first)
    bridge.queue_pause(instance)
    active_options = bridge.run(turn).snapshot['options']
    # A message without the package may add plain live input while retaining active context.
    different = context_package()
    different['exclusions'] = ['excluded-rule']
    different['selection_hash'] = digest({
        'context_refs': ['fixture:1'],
        'instructions': [{**item, 'assets': [{'path': asset['path'], 'digest': asset['digest']}
            for asset in item['assets']]} for item in different['instructions']],
        'exclusions': different['exclusions'], 'tools': []})
    denied = bridge.queue_add(instance, 'incompatible', context_package=different,
                              idempotency_key='selected-guidance')
    with pytest.raises(BridgeError) as failure:
        bridge.queue_dispatch(instance, denied['message_id'], mode='steer', expected_turn_id=turn)
    assert failure.value.code == 'steering_context_unsupported'
    assert bridge.message_get(denied['message_id'])['state'] == 'queued'
    with bridge.store.connect() as db:
        identity = tuple(db.execute('SELECT request_key,request_digest,options FROM queued_messages '
                                    'WHERE id=?', (denied['message_id'],)).fetchone())
    result = bridge.queue_dispatch(instance, denied['message_id'], mode='steer',
                                   expected_turn_id=turn, use_active_context=True)
    assert result['message_id'] == denied['message_id'] and result['turn_id'] == turn
    until(lambda: bridge.message_get(denied['message_id'])['state'] == 'delivered')
    assert bridge.queue_list(instance)['paused']
    assert bridge.run(turn).snapshot['options'] == active_options
    replay = bridge.queue_dispatch(instance, denied['message_id'], mode='steer',
                                   expected_turn_id=turn, use_active_context=True)
    assert replay['message_id'] == denied['message_id'] and replay['state'] == 'delivered'
    with bridge.store.connect() as db:
        assert tuple(db.execute('SELECT request_key,request_digest,options FROM queued_messages '
                                 'WHERE id=?', (denied['message_id'],)).fetchone()) == identity
    assert sum(event['kind'] == 'message.created' and event['message_id'] == denied['message_id']
               for event in bridge.turn_events(turn)) == 1
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
    bridge.queue_pause(instance)
    active_options = bridge.run(turn).snapshot['options']
    other = bridge.queue_add(instance, 'other', context_package=package,
        mcp={**descriptor, 'operation_id': str(uuid4()), 'socket_path': str(tmp_path / 'other.sock')})
    with pytest.raises(BridgeError) as failure:
        bridge.queue_dispatch(instance, other['message_id'], mode='steer', expected_turn_id=turn)
    assert failure.value.code == 'steering_context_unsupported'
    version = bridge.queue_list(instance)['version']
    for options, code in (({'mode': 'interrupt', 'expected_turn_id': turn}, 'invalid_request'),
                          ({'mode': 'steer'}, 'invalid_request'),
                          ({'mode': 'steer', 'expected_turn_id': 'other-turn'}, 'turn_conflict'),
                          ({'mode': 'steer', 'expected_turn_id': turn,
                            'expected_version': version + 1}, 'version_conflict')):
        with pytest.raises(BridgeError) as failure:
            bridge.queue_dispatch(instance, other['message_id'], use_active_context=True, **options)
        assert failure.value.code == code
        assert bridge.queue_list(instance)['version'] == version
        assert bridge.message_get(other['message_id'])['state'] == 'queued'
    settings = bridge.queue_add(instance, 'different settings', effort='high')
    with pytest.raises(BridgeError) as failure:
        bridge.queue_dispatch(instance, settings['message_id'], mode='steer',
                              expected_turn_id=turn, use_active_context=True)
    assert failure.value.code == 'steering_options_conflict'
    assert bridge.message_get(settings['message_id'])['state'] == 'queued'
    bridge.queue_dispatch(instance, other['message_id'], mode='steer',
                          expected_turn_id=turn, use_active_context=True)
    until(lambda: bridge.message_get(other['message_id'])['state'] == 'delivered')
    assert bridge.run(turn).snapshot['options'] == active_options
    finish = bridge.queue_add(instance, 'finish', context_package=package,
                              mcp={**descriptor, 'operation_id': str(uuid4())})
    bridge.queue_dispatch(instance, finish['message_id'], mode='steer', expected_turn_id=turn)
    until(lambda: bridge.message_get(finish['message_id'])['state'] == 'delivered')
    assert bridge.run(turn).wait(10)['state'] == 'completed'
