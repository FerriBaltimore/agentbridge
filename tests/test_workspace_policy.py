"""Reject workspaces that would grant Codex access to private bridge state."""

import pytest
import sys

from agentbridge import Account, Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.workspace_policy import validate_execution_workspace
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account


def test_instance_creation_rejects_private_state_before_routing(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    seed_authenticated_proxy_account(bridge.store, Account(
        'fixture', 'codex', provider='codex', supported_models=('fixture-model',),
        proxy_base_url='http://127.0.0.1:9/v1', key_env='FIXTURE_KEY',
        management_key_env='FIXTURE_MANAGEMENT'))
    for workspace in (tmp_path, tmp_path / 'state', '/proc'):
        with pytest.raises(BridgeError) as error:
            bridge.instance_create(model='fixture-model', workspace_path=workspace)
        assert error.value.code == 'invalid_workspace'
    with pytest.raises(BridgeError) as error:
        bridge.session('fixture', tmp_path, model='fixture-model')
    assert error.value.code == 'invalid_workspace'
    assert bridge.sessions() == []
    bridge.close()


def test_old_overlapping_session_is_rejected_before_turn_admission(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    seed_authenticated_proxy_account(bridge.store, Account(
        'fixture', 'codex', provider='codex', supported_models=('fixture-model',),
        proxy_base_url='http://127.0.0.1:9/v1', key_env='FIXTURE_KEY',
        management_key_env='FIXTURE_MANAGEMENT'))
    bridge.store.add_session('legacy', 'fixture', str(tmp_path), 'fixture-model')
    with pytest.raises(BridgeError) as error:
        bridge.submit('legacy', 'synthetic prompt',
                      options=RunOptions(model='fixture-model'))
    assert error.value.code == 'invalid_workspace'
    assert bridge.store.session_run_count('legacy') == 0
    bridge.close()


def test_default_root_requires_explicit_migration_of_project_state(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    legacy = workspace / '.agentbridge'
    legacy.mkdir(parents=True)
    (legacy / 'bridge.sqlite3').touch()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state-home'))
    with pytest.raises(BridgeError) as error:
        Bridge()
    assert error.value.code == 'state_migration_required'
    assert not (tmp_path / 'state-home/agentbridge').exists()


def test_writable_workspace_rejects_runtime_inside_project_before_admission(
        tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    bridge = Bridge(tmp_path / 'state')
    seed_authenticated_proxy_account(bridge.store, Account(
        'fixture', 'codex', provider='codex', supported_models=('fixture-model',),
        proxy_base_url='http://127.0.0.1:9/v1', key_env='FIXTURE_KEY',
        management_key_env='FIXTURE_MANAGEMENT'))
    bridge.store.add_session('instance', 'fixture', str(workspace), 'fixture-model')
    monkeypatch.setattr(sys, 'prefix', str(workspace / '.venv'))
    with pytest.raises(BridgeError) as error:
        bridge.submit('instance', 'synthetic prompt',
                      options=RunOptions(model='fixture-model', sandbox='workspace-write'))
    assert error.value.code == 'invalid_workspace'
    assert bridge.store.session_run_count('instance') == 0
    bridge.close()


def test_writable_workspace_rejects_pinned_proxy_binary(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    fake = workspace / 'cliproxy'
    fake.touch()
    monkeypatch.setenv('AGENTBRIDGE_CLIPROXY_BIN', str(fake))
    with pytest.raises(BridgeError) as error:
        validate_execution_workspace(workspace, tmp_path / 'state', workspace_write=True)
    assert error.value.code == 'invalid_workspace'
