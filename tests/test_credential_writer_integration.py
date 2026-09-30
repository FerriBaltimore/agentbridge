"""Actual CLIProxy binary and managed SDK with offline native execution in a private netns."""

import base64
from dataclasses import replace
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import time
from uuid import uuid4

import pytest

from agentbridge import Account, Bridge
from agentbridge.errors import BridgeError
from agentbridge.proxy.management import ManagementClient
from agentbridge.proxy.route import ProxyRoute
from agentbridge.proxy.process_lifecycle import _record_process_running
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account


def upload(route, suffix):
    claims = {'email': 'fixture@example.invalid',
              'https://api.openai.com/auth': {'chatgpt_account_id': 'fixture-identity',
                                            'chatgpt_plan_type': 'team'}}
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b'=').decode()
    data = {'type': 'codex', 'access_token': 'synthetic-' + suffix,
            'id_token': 'eyJhbGciOiJub25lIn0.' + encoded + '.sig',
            'expired': '2099-01-01T00:00:00Z'}
    port = int(route['proxy_base_url'].split(':')[2].split('/')[0])
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        connection.request('POST', '/v0/management/auth-files?name=fixture.json', json.dumps(data),
            {'Authorization': 'Bearer ' + os.environ[route['management_key_env']],
             'Content-Type': 'application/json'})
        response = connection.getresponse()
        assert response.status == 200, 'Synthetic management upload failed'
        response.read()
    finally:
        connection.close()


def test_real_writer_snapshot_preserves_active_native_turn_and_detects_rotation(tmp_path, monkeypatch):
    binary = os.environ.get('BD2_CLIPROXY_BIN')
    if not binary:
        pytest.skip('Select the separately built CLIProxy writer candidate')
    assert [name for _, name in socket.if_nameindex()] == ['lo'], 'Require loopback-only netns'
    assert not Path('/proc/net/route').read_text().splitlines()[1:], 'Require loopback-only netns'
    wrapper = tmp_path / 'cliproxy'
    wrapper.write_text(f'#!{sys.executable}\nimport os,sys\n'
                       f'os.execv({binary!r}, [__file__, "--local-model", *sys.argv[1:]])\n')
    wrapper.chmod(0o700)
    monkeypatch.setenv('AGENTBRIDGE_CLIPROXY_BIN', str(wrapper))
    monkeypatch.setenv('HOME', str(tmp_path))
    native = tmp_path / 'native'
    native.write_text('#!/usr/bin/env python3\n' + (
        Path(__file__).parent / 'fixtures/test_durable_codex.py').read_text())
    native.chmod(0o700)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    gate = workspace / 'release'
    bridge = Bridge(tmp_path / 'state', owner_ref='fixture-owner', durable=True)
    try:
        route = bridge.managed_proxy.provision('fixture')
        upload(route, 'initial')
        management = ManagementClient(ProxyRoute('fixture', route['proxy_base_url'], route['key_env']),
                                      route['management_key_env'])
        until = time.monotonic() + 5
        observation = management.observe()
        while not observation['models'] and time.monotonic() < until:
            time.sleep(.05)
            observation = management.observe()
        assert observation['models'], 'Embedded model catalogue must be available offline'
        model = observation['models'][0]['id']
        account = Account('fixture', 'codex', provider='codex', supported_models=(model,), **route)
        seed_authenticated_proxy_account(bridge.store, replace(account, command=(str(native),)),
                                         observe_local=True)
        for instance in ('active', 'finished'):
            bridge.store.add_session(instance, 'fixture', str(workspace), model)
        first = bridge.run(bridge.message_create('active', 'hold:' + str(gate))['turn_id'])
        second = bridge.run(bridge.message_create('finished', 'independent')['turn_id'])
        assert second.wait(15)['state'] == 'completed'
        assert list(second.events())[-1].kind == 'checkpoint_ready'
        record_path = bridge.root / 'managed-proxies/accounts/fixture/route.json'
        before = json.loads(record_path.read_text())
        assert first.status in {'starting', 'running'}
        descriptor = bridge.credential_snapshots.capture_online(
            operation_id=str(uuid4()), account_ids=['fixture'])
        assert bridge.credential_snapshots.verify_current(
            snapshot_id=descriptor['snapshot_id'])['current']
        assert json.loads(record_path.read_text()) == before and _record_process_running(before)
        assert first.status in {'starting', 'running'}
        upload(route, 'rotated')
        with pytest.raises(BridgeError) as changed:
            bridge.credential_snapshots.verify_current(snapshot_id=descriptor['snapshot_id'])
        assert changed.value.code == 'credential_snapshot_pending'
        latest = bridge.credential_snapshots.capture_online(
            operation_id=str(uuid4()), account_ids=['fixture'])
        assert bridge.credential_snapshots.verify_current(snapshot_id=latest['snapshot_id'])['current']
        gate.touch()
        assert first.wait(15)['state'] == 'completed'
        assert list(first.events())[-1].kind == 'checkpoint_ready'
        assert json.loads(record_path.read_text()) == before and _record_process_running(before)
    finally:
        gate.touch()
        bridge.close(cancel=True)
        bridge.managed_proxy.shutdown()
