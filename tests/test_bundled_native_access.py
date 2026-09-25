"""Actual bundled Codex executes bounded canaries under each native policy."""

from pathlib import Path
import json
import os
import re
import shlex
import subprocess
import sys

import pytest

from agentbridge import Account, Bridge, RunOptions
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from fixtures.test_responses_server import responses_server
from test_bundled_native_continuity import pytestmark


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
