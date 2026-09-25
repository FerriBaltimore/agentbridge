"""Queue HTTP mutations use only the public SDK and preserve conflict fences."""

import pytest

from playground.server import API_REVISION
from test_playground_server import local_server, request


@pytest.mark.parametrize('action,values,method', [
    ('move', {'message_id': 'message-1', 'position': 0, 'expected_version': 7}, 'queue_move'),
    ('delete', {'message_id': 'message-1', 'expected_version': 8}, 'queue_delete'),
    ('dispatch', {'message_id': 'message-1', 'mode': 'steer', 'expected_turn_id': 'turn-1',
                  'expected_version': 9}, 'queue_dispatch'),
    ('dispatch', {'message_id': 'message-1', 'mode': 'interrupt'}, 'queue_dispatch'),
    ('pause', {'expected_version': 10}, 'queue_pause'),
    ('resume', {'expected_version': 11}, 'queue_resume'),
])
def test_queue_mutation_reaches_public_sdk(local_server, action, values, method):
    server, bridge = local_server
    def record(instance_id, **options):
        bridge.calls.append((method, instance_id, options))
        return {'version': 12}
    setattr(bridge, method, record)
    status, result = request(server, 'POST', f'/api/instances/instance-1/queue/{action}', body=values)
    assert status == 200 and result['result'] == {'version': 12}
    assert bridge.calls == [(method, 'instance-1', values)]


def test_queue_snapshot_and_pending_message_receipt(local_server):
    server, bridge = local_server
    def listing(instance_id, *, limit):
        assert instance_id == 'instance-1' and limit == 1000
        return {'items': [], 'total': 0, 'version': 1, 'paused': False}
    bridge.queue_list = listing
    assert request(server, 'GET', '/api/instances/instance-1/queue')[1]['result']['version'] == 1
    values = {'content': 'Pending input', 'delivery': 'queue', 'idempotency_key': 'message-key'}
    assert request(server, 'POST', '/api/instances/instance-1/messages', body=values)[0] == 200
    assert bridge.calls == [('message_create', ('instance-1',), values)]


@pytest.mark.parametrize('action,values', [
    ('move', {'message_id': 'message-1'}),
    ('delete', {'message_id': 'message-1', 'position': 0}),
    ('dispatch', {'message_id': 'message-1'}),
    ('resume', {'unexpected': True}),
])
def test_queue_rejects_malformed_requests_without_calling_sdk(local_server, action, values):
    server, bridge = local_server
    assert request(server, 'POST', f'/api/instances/instance-1/queue/{action}', body=values)[0] == 400
    assert bridge.calls == []


def test_old_page_cannot_mutate_queue(local_server):
    server, bridge = local_server
    status, result = request(server, 'POST', '/api/instances/instance-1/queue/pause',
                            body={}, csrf=False, headers={'X-AgentBridge-Playground': '1',
                              'X-AgentBridge-API-Revision': str(API_REVISION - 1)})
    assert status == 409 and result['error']['code'] == 'playground_update_required'
    assert bridge.calls == []
