"""Authentication spawn failures remain visible on persistent replay."""
import json

import pytest

from agentbridge import Bridge
from agentbridge import auth_runtime
from agentbridge.errors import BridgeError


def test_auth_spawn_failure_persists_failed_attempt_for_replay(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / 'state')
    attempt = {'id': 'fixture-attempt', 'owner': 'fixture-owner', 'account_id': 'fixture-account',
               'engine': 'claude', 'name': 'Fixture', 'email': None, 'mode': 'browser',
               'browser': 'same_host', 'status': 'starting', 'data': {}}
    bridge.store.create_auth_attempt(attempt, request_key='fixture-key', connection={})
    spawned = []
    def fail(*_, **kwargs):
        spawned.append(True)
        raise OSError('PRIVATE EXECUTABLE FAILURE')
    monkeypatch.setattr(auth_runtime.subprocess, 'Popen', fail)
    with pytest.raises(BridgeError) as failure:
        bridge.authentication.runtime.launch(attempt['id'], 'start')
    assert failure.value.code == 'launch_failed'
    row = bridge.store.get_auth_attempt(attempt['id'])
    assert row['status'] == 'failed'
    assert row['data']['error']['code'] == 'launch_failed'
    replay, created = bridge.store.create_auth_attempt(attempt, request_key='fixture-key', connection={})
    assert created is False and replay['status'] == 'failed'
    assert len(spawned) == 1
    assert not bridge.authentication.runtime.active(bridge.authentication.runtime.get(attempt['id']))
    assert 'PRIVATE EXECUTABLE FAILURE' not in json.dumps(row)
