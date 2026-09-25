"""Reject incompatible chat clients before they can mutate SDK state."""

import pytest

from playground.server import API_REVISION
from test_playground_server import local_server, request


@pytest.mark.parametrize('revision', [None, '0', str(API_REVISION + 1), 'invalid'])
@pytest.mark.parametrize('method,path,body', [
    ('POST', '/api/instances', {'model': 'fixture-model'}),
    ('POST', '/api/instances/instance-1', {'expected_version': 1}),
    ('POST', '/api/instances/instance-1/messages', {'content': 'Keep this draft'}),
    ('DELETE', '/api/instances/instance-1', None),
])
def test_incompatible_chat_request_never_reaches_sdk(local_server, revision, method, path, body):
    server, bridge = local_server
    headers = {'X-AgentBridge-Playground': '1'}
    if revision is not None:
        headers['X-AgentBridge-API-Revision'] = revision
    status, result = request(server, method, path, body=body, headers=headers, csrf=False)
    assert status == 409
    assert result['error']['code'] == 'playground_update_required'
    assert bridge.calls == []


def test_contract_revision_is_advertised_and_compatible_requests_execute_once(local_server):
    server, bridge = local_server
    assert request(server, 'GET', '/api/meta')[1]['result']['api_revision'] == API_REVISION
    values = {'model': 'fixture-model', 'routing_mode': 'automatic',
              'permission_mode': 'dontAsk', 'sandbox_mode': 'read-only'}
    status, _ = request(server, 'POST', '/api/instances', body=values)
    assert status == 200
    assert bridge.calls == [('instance_create', (), {
        **values, 'workspace_path': server.workspace_path})]


def test_stop_and_permission_decisions_remain_available_during_updates(local_server):
    server, bridge = local_server
    headers = {'X-AgentBridge-Playground': '1'}
    assert request(server, 'POST', '/api/turns/turn-1/stop', body={'wait': False},
                   headers=headers, csrf=False)[0] == 200
    assert request(server, 'POST', '/api/turns/turn-1/permissions/permission-1',
                   body={'decision': 'deny'}, headers=headers, csrf=False)[0] == 200
    assert [call[0] for call in bridge.calls] == ['turn_stop', 'permission_respond']
