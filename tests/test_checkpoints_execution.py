"""Real detached workers against an offline provider, including independent session lifetimes."""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from agentbridge import Bridge
from fixtures.test_proxy_account_fixture import proxy_account, seed_authenticated_proxy_account
from test_v2_routing_integration import management_server


def test_two_workers_share_proxy_but_seal_and_resume_independent_native_homes(tmp_path, monkeypatch):
    fixture = tmp_path / 'codex-fixture'
    fixture.write_text('#!/usr/bin/env python3\n' + (
        Path(__file__).parent / 'fixtures/test_durable_codex.py').read_text())
    fixture.chmod(0o700)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setenv('FIXTURE_PROXY_KEY', 'local-client-fixture')
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', 'local-management-fixture')
    state = {'used': 0, 'identity': 'offline-fixture', 'models': ['fixture-model']}
    with management_server(state) as endpoint:
        bridge = Bridge(tmp_path / 'state', owner_ref='fixture-owner', durable=True)
        account = replace(proxy_account('fixture', 1, provider='codex'),
                          proxy_base_url=endpoint, command=(str(fixture),))
        account = seed_authenticated_proxy_account(bridge.store, account, observe_local=True)
        for name in ('one', 'two'):
            bridge.store.add_session(name, 'fixture', str(workspace), 'fixture-model')
        gate = workspace / 'release'
        try:
            accepted = bridge.message_create('one', 'hold:' + str(gate))
            first = bridge.run(accepted['turn_id'])
            accepted = bridge.message_create('two', 'independent')
            second = bridge.run(accepted['turn_id'])
            assert second.wait(15)['state'] == 'completed', second.snapshot
            assert first.status in {'running', 'starting'}
            events = list(second.events())
            assert events[-1].kind == 'checkpoint_ready', [event.data for event in events]
            # Both workers read this same external proxy fixture; sealing one must not kill it.
            assert bridge.routes.observe(account)['status'] == 'active'
            gate.touch()
            assert first.wait(15)['state'] == 'completed', first.snapshot
            assert list(first.events())[-1].kind == 'checkpoint_ready'
            answer = json.loads(second.text)
            third = bridge.run(bridge.message_create('two', 'resume existing')['turn_id'])
            assert third.wait(15)['state'] == 'completed', third.snapshot
            continuation = json.loads(third.text)
            assert continuation['native_id'] == answer['native_id']
            assert continuation['previous'] == ['independent']
            assert continuation['resumed'] is True
            assert list(third.events())[-1].kind == 'checkpoint_ready'
        finally:
            gate.touch()
            bridge.close(cancel=True)


def test_subreaper_kills_escaped_owned_descendant_and_preserves_unrelated_process(tmp_path):
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    script = Path(__file__).parent / 'fixtures/test_checkpoint_descendants.py'
    try:
        result = subprocess.run([sys.executable, str(script), str(tmp_path)],
                                capture_output=True, text=True, timeout=10,
                                env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')})
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {'quiesced': True, 'escaped_stopped': True}
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)
