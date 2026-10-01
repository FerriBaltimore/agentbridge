"""Private local HTTP MCP and read projections use the existing execution boundary."""

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from uuid import uuid4

import pytest

from agentbridge.codex_control import CodexControl
from agentbridge.errors import BridgeError
from agentbridge.execution_context import (
    codex_config, mcp_endpoint_digest, mcp_environment, prepare, verify,
)
from agentbridge.models import RunOptions
from agentbridge.native_namespace import readonly_projections
from agentbridge.native_sandbox import wrap
from test_interactive_inputs import context_package


def descriptor():
    return {'version': 2, 'operation_id': str(uuid4()), 'capability': 'PRIVATE_TOKEN_12345678',
            'servers': [{'name': 'connection-one', 'url': 'http://127.0.0.1:18081/mcp/one'}]}


def options(package, mcp):
    execution, context, binding = prepare(package, mcp)
    return execution, RunOptions(sandbox='danger-full-access', host_isolated=True,
        permission_mode='dontAsk', context_package_digest=context, mcp_binding_digest=binding,
        mcp_endpoint_digest=mcp_endpoint_digest(mcp))


class Channel:
    def __init__(self):
        self.sent, self.pending = [], []

    def send(self, value):
        self.sent.append(value)
        if 'id' not in value:
            return
        method = value['method']
        result = ({'thread': {'id': 'native-fixture'}} if method.startswith('thread/') else
                  {'turn': {'id': 'turn-fixture'}} if method == 'turn/start' else {})
        self.pending.append({'id': value['id'], 'result': result})
        if method == 'turn/start':
            self.pending.append({'method': 'turn/completed', 'params': {
                'threadId': 'native-fixture', 'turn': {'id': 'turn-fixture',
                                                       'status': 'completed'}}})

    def receive(self, timeout):
        return self.pending.pop(0)


def test_http_config_reaches_start_and_resume_without_changing_native_identity():
    mcp, seen = descriptor(), []
    for native_id in (None, 'native-fixture'):
        execution, policy = options(None, mcp)
        verify(policy, execution)
        config = codex_config(mcp_enabled=True, host_isolated=True, mcp=mcp)
        channel = Channel()
        payload = {'options': asdict(policy), 'cwd': '/workspace', 'prompt': 'fixture',
                   'native_id': native_id, 'codex_config': config, 'mcp': mcp,
                   'mcp_enabled': True}
        CodexControl(channel, payload, seen.append, lambda *_: pytest.fail('approval')).execute()
        start = next(row for row in channel.sent if row['method'].startswith('thread/'))
        assert start['method'] == ('thread/resume' if native_id else 'thread/start')
        assert start['params']['approvalPolicy'] == 'never'
        servers = start['params']['config']['mcp_servers']
        assert len(servers) == len(mcp['servers'])
        assert all(row['default_tools_approval_mode'] == 'approve' for row in servers.values())
        assert 'agentbridge_execution' not in servers
        assert 'AGENTBRIDGE_MCP_SOCKET_PATH' not in mcp_environment(mcp)
        assert mcp['capability'] not in json.dumps([asdict(policy), seen])
        mcp = {**mcp, 'operation_id': str(uuid4()), 'servers': [*mcp['servers'],
            {'name': 'connection-two', 'url': 'http://127.0.0.1:18081/mcp/two'}]}


def test_rebind_rotates_only_capability_and_rejects_changed_endpoints_and_roots():
    package, mcp = context_package(), descriptor()
    package['read_only_paths'] = ['/workspace/accepted/chat']
    execution, policy = options(package, mcp)
    refreshed = deepcopy(execution)
    refreshed['mcp']['capability'] = 'ANOTHER_PRIVATE_TOKEN_12345678'
    verify(policy, refreshed)
    refreshed['mcp']['servers'][0]['url'] += '/other'
    with pytest.raises(BridgeError, match='does not match'):
        verify(policy, refreshed)
    refreshed = deepcopy(execution)
    refreshed['context_package']['read_only_paths'] = ['/workspace/accepted/other']
    with pytest.raises(BridgeError, match='does not match'):
        verify(policy, refreshed)
    package['execution_mode'] = 'inputs_only'
    with pytest.raises(BridgeError):
        prepare(package, None)
    with pytest.raises(BridgeError):
        wrap(['/usr/bin/true'], inputs_only=True, read_only_paths=['/workspace/accepted'])


def test_http_descriptor_rejects_external_unbounded_or_unreviewed_configuration():
    for change in (
        {'servers': [{'name': 'a', 'url': 'http://example.com/mcp'}]},
        {'servers': [{'name': 'a', 'url': 'http://127.0.0.1:18081/mcp?token=x'}]},
        {'servers': []}, {'servers': descriptor()['servers'] * 101},
        {'config': {'approval_policy': 'never'}},
    ):
        with pytest.raises(BridgeError):
            prepare(None, descriptor() | change)
    _, policy = options(None, descriptor())
    legacy = {'version': 1, 'operation_id': str(uuid4()), 'socket_path': '/tmp/tools.sock',
              'capability': 'LEGACY_PRIVATE_12345678'}
    assert mcp_environment(legacy)['AGENTBRIDGE_MCP_SOCKET_PATH'] == '/tmp/tools.sock'
    assert 'agentbridge_execution' in codex_config(mcp_enabled=True)['mcp_servers']


def test_required_http_startup_failure_is_not_silently_ignored():
    native = CodexControl(None, {'mcp_enabled': True, 'mcp': descriptor()}, lambda _: None, None)
    with pytest.raises(BridgeError) as failure:
        native.event({'method': 'mcpServer/startupStatus/updated', 'params': {
            'name': 'connection-one', 'status': 'failed', 'error': 'PRIVATE_UPSTREAM'}})
    assert failure.value.code == 'provider_unavailable'
    assert failure.value.outcome == 'not_started'
    assert 'PRIVATE_UPSTREAM' not in str(failure.value)


def test_projection_is_canonical_and_separate_from_cwd_native_state_and_system(tmp_path):
    cwd, home, temp, view = [tmp_path / name for name in
                            ('cwd', 'state/codex-runtime/chat', 'temporary', 'projection')]
    for path in (cwd, home, temp, view):
        path.mkdir(parents=True)
    assert readonly_projections([view], cwd=cwd, home=home, temporary=temp) == [view]
    for unsafe in (cwd, home, temp, tmp_path, Path('/'), Path('/proc'), Path('/usr')):
        with pytest.raises(ValueError):
            readonly_projections([unsafe], cwd=cwd, home=home, temporary=temp)
    linked = tmp_path / 'link'
    linked.symlink_to(view, target_is_directory=True)
    with pytest.raises(ValueError):
        readonly_projections([linked], cwd=cwd, home=home, temporary=temp)


def test_host_readonly_workspace_survives_full_access_and_changes_are_bound_to_turn():
    package = context_package()
    package['workspace_write'] = False
    execution, policy = options(package, descriptor())
    verify(policy, execution)
    command = wrap(['/usr/bin/true'], full_access=True, host_isolated=True,
                   native_workspace_write=False)
    assert '--read-only-workspace' in command and '--host-isolated' in command
    execution['context_package']['workspace_write'] = True
    with pytest.raises(BridgeError, match='does not match'):
        verify(policy, execution)
    with pytest.raises(BridgeError):
        wrap(['/usr/bin/true'], full_access=True, native_workspace_write=False)
