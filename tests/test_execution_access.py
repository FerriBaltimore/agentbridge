"""Exercise actual access and context isolation, not just accepted option names."""

import json
from pathlib import Path

import pytest

from agentbridge import RunOptions
from agentbridge.errors import BridgeError
from agentbridge.execution_context import validate_access
from agentbridge.native_sandbox import wrap
from test_interactive_inputs import context_package, setup_proxy


FIXTURE = Path(__file__).parent / 'fixtures/test_access_provider.py'


@pytest.mark.parametrize('permission_mode', ['dontAsk', 'default'], ids=['exec', 'app-server'])
@pytest.mark.parametrize('sandbox', ['read-only', 'workspace-write', 'danger-full-access'])
def test_effective_filesystem_access_through_sdk(setup_proxy, tmp_path, sandbox, permission_mode):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    inside, outside = workspace / 'inside', tmp_path / 'outside'
    for path in (inside, outside):
        path.write_text('synthetic fixture')
    bridge, instance = setup_proxy(workspace_path=workspace,
        native_command=('/usr/bin/python3', '-c', FIXTURE.read_text()))
    accepted = bridge.message_create(instance, json.dumps({
        'inside': str(inside), 'outside': str(outside)}),
        sandbox_mode=sandbox, permission_mode=permission_mode)
    run = bridge.run(accepted['turn_id'])
    assert run.wait(10)['state'] == 'completed', run.snapshot
    assert json.loads(run.text) == {
        'inside_read': True,
        'inside_write': sandbox != 'read-only',
        'outside_read': sandbox == 'danger-full-access',
        'outside_write': sandbox == 'danger-full-access',
    }
    observed = next(e['data'] for e in bridge.turn_events(run.id) if e['kind'] == 'run.started')
    assert observed['sandbox_mode'] == sandbox
    assert observed['permission_mode'] == permission_mode


@pytest.mark.parametrize('delivery', ['reject', 'queue', 'steer', 'interrupt'])
def test_full_access_cannot_disable_selected_context_isolation(setup_proxy, delivery):
    bridge, instance = setup_proxy()
    with pytest.raises(BridgeError) as error:
        bridge.message_create(instance, 'synthetic request', sandbox_mode='danger-full-access',
                              context_package=context_package(), delivery=delivery)
    assert error.value.code == ('turn_not_active' if delivery == 'steer'
                               else 'invalid_execution_policy')
    assert bridge.store.session_run_count(instance) == 0
    assert bridge.messages(instance) == []


@pytest.mark.parametrize('field', ['context_package_digest', 'mcp_binding_digest'])
def test_full_access_rejects_private_input_boundaries(field):
    with pytest.raises(BridgeError) as error:
        validate_access(RunOptions(sandbox='danger-full-access', **{field: 'a' * 64}))
    assert error.value.code == 'invalid_execution_policy'


@pytest.mark.parametrize('mode', ['inputs_only', 'mcp_enabled', 'selected_context'])
def test_native_launcher_cannot_bypass_selected_isolation(mode):
    with pytest.raises(BridgeError) as error:
        wrap(['/usr/bin/true'], full_access=True, **{mode: True})
    assert error.value.code == 'invalid_execution_policy'
