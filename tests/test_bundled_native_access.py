"""Actual bundled Codex executes bounded canaries under each native policy."""

from pathlib import Path
import json
import os
import re
import shlex
import subprocess
import sys
from threading import Event

import pytest

from agentbridge import Account, Bridge, RunOptions
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from fixtures.test_responses_server import responses_server
from test_bundled_native_continuity import pytestmark
from test_message_queues import until


@pytest.fixture
def external_process():
    """A same-user process containing only a synthetic environment canary."""
    process = subprocess.Popen(
        [sys.executable, '-I', '-c', 'import time; time.sleep(60)'],
        env={'PATH': os.defpath, 'FIXTURE_NATIVE_WORKER_SECRET': 'synthetic worker canary'},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield process
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.parametrize('steerable', [False, True], ids=['exec', 'app-server'])
@pytest.mark.parametrize('sandbox', ['read-only', 'workspace-write', 'danger-full-access'])
def test_native_tool_effective_access(tmp_path, monkeypatch, sandbox, steerable, external_process):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    inside, outside = workspace / 'inside.txt', tmp_path / 'outside.txt'
    inside.write_text('synthetic native canary')
    outside.write_text('synthetic native canary')
    script = workspace / 'native_probe.py'
    script.write_text((Path(__file__).parent / 'fixtures/test_native_access_probe.py').read_text())
    with responses_server('native', 'codex', ('fixture-model',)) as (network_endpoint, _):
        tool_command = shlex.join(['/usr/bin/python3', str(script), str(inside), str(outside),
                                  network_endpoint + '/models', str(external_process.pid)])
        with responses_server('native', 'codex', ('fixture-model',),
                              tool_command=tool_command) as (endpoint, observations):
            monkeypatch.setenv('FIXTURE_NATIVE_ACCESS_KEY', 'fixture-access-key')
            monkeypatch.setenv('FIXTURE_NATIVE_ACCESS_MANAGEMENT', 'fixture-access-management')
            bridge = Bridge(tmp_path / 'state')
            try:
                seed_authenticated_proxy_account(bridge.store, Account(
                    'native', 'codex', provider='codex', supported_models=('fixture-model',),
                    proxy_base_url=endpoint, key_env='FIXTURE_NATIVE_ACCESS_KEY',
                    management_key_env='FIXTURE_NATIVE_ACCESS_MANAGEMENT'), observe_local=True)
                instance = bridge.instance_create(model='fixture-model', account_ref='native',
                                                   workspace_path=workspace)
                run = bridge.submit(instance['id'], 'fixture request permission canaries',
                                    options=RunOptions(sandbox=sandbox, permission_mode='dontAsk',
                                                       steerable=steerable, timeout=30))
                assert run.wait(40)['state'] == 'completed', run.snapshot
                assert len(observations) == 2
                assert observations[-1]['tool_outputs'], observations
                expected_inside = 'synthetic native canary' if sandbox == 'read-only' else 'synthetic native write'
                expected_outside = 'synthetic native write' if sandbox == 'danger-full-access' else 'synthetic native canary'
                assert inside.read_text() == expected_inside, observations[-1]['tool_outputs']
                assert outside.read_text() == expected_outside, observations[-1]['tool_outputs']
                tool_output = '\n'.join(str(item) for item in observations[-1]['tool_outputs'])
                marker = re.search(r'^FIXTURE_NATIVE_ACCESS=(\{.*\})$', tool_output, re.MULTILINE)
                assert marker, tool_output
                assert json.loads(marker.group(1)) == {
                    'inside_read': True,
                    'inside_write': sandbox != 'read-only',
                    'outside_read': sandbox == 'danger-full-access',
                    'outside_write': sandbox == 'danger-full-access',
                    'loopback_network': sandbox == 'danger-full-access',
                    'outside_process_visible': sandbox == 'danger-full-access',
                    'outside_process_environment_readable': sandbox == 'danger-full-access',
                }
            finally:
                bridge.close(cancel=True)


def test_native_tool_start_and_interrupt_are_observable_before_completion(tmp_path, monkeypatch):
    release = Event()
    def hold_answer(_request, observation):
        if observation['tool_outputs']:
            assert release.wait(20)
        return None
    with responses_server('native', 'codex', ('fixture-model',),
                          tool_command="printf 'native partial\\n'; sleep 30",
                          select_events=hold_answer) as (endpoint, observations):
        monkeypatch.setenv('FIXTURE_NATIVE_LIVE_KEY', 'fixture-live-key')
        monkeypatch.setenv('FIXTURE_NATIVE_LIVE_MANAGEMENT', 'fixture-live-management')
        bridge = Bridge(tmp_path / 'state')
        workspace = tmp_path / 'workspace'
        workspace.mkdir()
        try:
            seed_authenticated_proxy_account(bridge.store, Account(
                'native', 'codex', provider='codex', supported_models=('fixture-model',),
                proxy_base_url=endpoint, key_env='FIXTURE_NATIVE_LIVE_KEY',
                management_key_env='FIXTURE_NATIVE_LIVE_MANAGEMENT'), observe_local=True)
            instance = bridge.instance_create(model='fixture-model', account_ref='native',
                                              workspace_path=workspace)['instance_id']
            started = bridge.message_create(instance, 'fixture request live output',
                                             permission_mode='default', timeout_ms=30000)
            turn_id = started['turn_id']
            tool = until(lambda: next((event for event in bridge.turn_events(turn_id)
                if event['kind'] == 'item.started' and
                event['data']['item']['type'] == 'command_execution'), None), timeout=15)
            events = bridge.turn_events(turn_id)
            assert tool['data']['item']['status'] == 'in_progress'
            assert not any(event['kind'] == 'turn.completed' for event in events)
            assert bridge.instance_read(instance)['status']['type'] == 'active'
            assert bridge.turn_interrupt(turn_id)['acknowledged']
            until(lambda: any(event['kind'] == 'turn.completed' and
                              event['data']['turn']['status'] == 'interrupted'
                              for event in bridge.turn_events(turn_id)))
            assert len(bridge.runs()) == 1
        finally:
            release.set()
            bridge.close(cancel=True)
