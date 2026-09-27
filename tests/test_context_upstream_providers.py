"""Selected context reaches every upstream provider through the one Codex transport.

An account's `provider` (`codex`, `claude` or `grok`) names the upstream OAuth
credential behind its dedicated CLIProxyAPI sidecar. It never selects a second
execution engine: every proxied account runs the Codex CLI, so the context
package, skills, evidence and per-turn overrides use the same app-server path.
"""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agentbridge import Account, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.transports import command, duplex, require_proxy_account
from test_interactive_inputs import context_package, setup_proxy  # noqa: F401


SOURCE_ROOT = Path(__file__).resolve().parents[1] / 'src'
ROUTE = {'proxy_base_url': 'http://127.0.0.1:8317/v1', 'key_env': 'CLIENT_KEY',
         'management_key_env': 'MANAGEMENT_KEY'}


def _account(provider):
    return Account('upstream', 'codex', provider=provider,
                   supported_models=('fixture-model',), **ROUTE)


@pytest.mark.parametrize('setup_proxy', ['claude', 'grok'], indirect=True)
def test_context_package_reaches_claude_and_grok_upstream_accounts(setup_proxy):
    bridge, instance = setup_proxy()
    account = bridge.account(bridge.get_session(instance)['account_id'])
    assert account.engine == 'codex' and account.provider in {'claude', 'grok'}
    accepted = bridge.message_create(instance, 'inspect-context', effort='high',
                                     context_window=131072,
                                     context_package=context_package())
    run = bridge.run(accepted['turn_id'])
    assert run.wait(10)['state'] == 'completed', run.snapshot
    assert json.loads(run.text) == {
        'developer': True, 'evidence': True, 'skill': True, 'mcp': False,
        'shell_disabled': True, 'skills_isolated': True, 'project_docs_disabled': True,
        'web_disabled': True, 'ephemeral': False, 'model': 'fixture-model',
        'effort': 'high', 'route': True, 'context_window': 'model_context_window=131072',
    }
    started = next(e['data'] for e in bridge.turn_events(run.id) if e['kind'] == 'run.started')
    assert started['engine'] == 'codex' and started['account_id'] == account.id
    assert not list((bridge.root / 'codex-runtime' / instance / 'skills')
                    .glob('agentbridge-context-*'))
    saved = json.dumps([run.snapshot, bridge.turn_events(run.id)])
    assert 'selected fixture rule' not in saved and 'fixture-client-key' not in saved


@pytest.mark.parametrize('provider', ['codex', 'claude', 'grok'])
def test_transport_uses_the_interactive_worker_for_every_proxied_provider(provider):
    account = _account(provider)
    session = {'native_id': None, 'model': 'fixture-model'}
    plain = RunOptions()
    selected = RunOptions(context_package_digest='a' * 64, context_window=32768, effort='high')
    assert duplex(account, plain) is False
    assert duplex(account, selected) is True
    assert duplex(account, RunOptions(mcp_binding_digest='b' * 64)) is True
    worker = command(account, session, selected)
    assert worker[-2:] == ['-m', 'agentbridge.interactive_worker']
    native = command(account, session, selected, native_transport=True)
    assert native[1:3] == ['app-server', '--stdio']
    assert 'model_providers.agentbridge_local_proxy.base_url="http://127.0.0.1:8317/v1"' in native
    assert 'model_providers.agentbridge_local_proxy.env_key="CLIENT_KEY"' in native
    assert 'model_context_window=32768' in native
    exec_path = command(account, session, plain)
    assert exec_path[1] == 'exec' and exec_path[exec_path.index('--model') + 1] == 'fixture-model'


@pytest.mark.parametrize('provider', ['claude', 'grok'])
def test_upstream_provider_without_proxy_route_fails_before_any_launch(provider, tmp_path):
    with pytest.raises(BridgeError) as error:
        Account('direct', 'codex', home=str(tmp_path), provider=provider,
                supported_models=('fixture-model',))
    assert error.value.code == 'invalid_proxy_account'
    legacy = SimpleNamespace(engine=provider, proxy_base_url=None)
    with pytest.raises(BridgeError) as error:
        require_proxy_account(legacy)
    assert error.value.code == 'invalid_proxy_account'
    with pytest.raises(BridgeError) as error:
        command(legacy, {'model': 'fixture-model'}, RunOptions(context_package_digest='a' * 64))
    assert error.value.code == 'invalid_proxy_account'
    # A duplex payload naming another engine is refused before any native process.
    payload = {'engine': provider, 'prompt': 'hello', 'cwd': str(tmp_path),
               'options': {'permission_mode': 'dontAsk', 'timeout': 5, 'sandbox': 'read-only'},
               'root': str(tmp_path / 'state'), 'turn_id': 'turn', 'command': ['/usr/bin/false'],
               'secret_names': [], 'context_package': None, 'mcp_enabled': False}
    result = subprocess.run(
        [sys.executable, '-P', '-m', 'agentbridge.interactive_worker'], input=json.dumps(payload),
        capture_output=True, text=True, timeout=30,
        env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(SOURCE_ROOT),
             'CODEX_HOME': str(tmp_path / 'home'), 'HOME': str(tmp_path / 'home')})
    assert result.returncode == 1
    emitted = json.loads(result.stdout.strip().splitlines()[-1])
    assert emitted['type'] == 'bridge_error' and emitted['outcome'] == 'not_started'
    assert emitted['error']['code'] == 'invalid_proxy_account'
    assert not (tmp_path / 'home').exists()
