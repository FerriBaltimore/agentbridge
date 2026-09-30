"""Native checkpoint acceptance with the real pinned Codex and an offline Responses fixture."""

from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

import pytest

from agentbridge import Account, Bridge
from agentbridge.errors import BridgeError
from agentbridge.checkpoint.snapshot import restore_store
from agentbridge.security import base_environment
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from fixtures.test_responses_server import responses_server
from test_bundled_native_continuity import native_history, pytestmark
from fixtures.test_checkpoint_fixture import scope, terminal


@pytest.mark.parametrize('permission_mode', ['dontAsk', 'default'])
def test_real_codex_checkpoint_restores_index_and_continues_same_native_id(
        tmp_path, monkeypatch, permission_mode):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    with ExitStack() as stack:
        endpoint, observed = stack.enter_context(responses_server('fixture', 'codex', ('fixture-model',)))
        monkeypatch.setenv('FIXTURE_NATIVE_KEY', 'offline-client')
        monkeypatch.setenv('FIXTURE_NATIVE_MANAGEMENT', 'offline-management')
        bridge = Bridge(tmp_path / 'source', owner_ref='fixture-owner', durable=True)
        stack.callback(bridge.close, cancel=True)
        seed_authenticated_proxy_account(bridge.store, Account(
            'fixture', 'codex', provider='codex', supported_models=('fixture-model',),
            proxy_base_url=endpoint, key_env='FIXTURE_NATIVE_KEY',
            management_key_env='FIXTURE_NATIVE_MANAGEMENT'), observe_local=True)
        instance = bridge.instance_create(model='fixture-model', account_ref='fixture',
                                          workspace_path=workspace)['id']
        turn = bridge.message_create(instance, 'fixture request 1',
                                     permission_mode=permission_mode, timeout_ms=30000)['turn_id']
        assert bridge.run(turn).wait(40)['state'] == 'completed'
        result = terminal(bridge, turn).data['durability']
        assert result['state'] == 'sealed', result
        checkpoint = result['checkpoint']
        snapshot = bridge.checkpoints.snapshot_store(format_version='1', operation_id=str(uuid4()),
                                                    params=scope(bridge))
        destination_workspace = tmp_path / 'restored-workspace'
        destination_workspace.mkdir(mode=0o700)
        binding = restore_store(tmp_path / 'destination', snapshot,
                                bridge.checkpoints.resolve_content(snapshot['content']),
                                owner_ref='fixture-owner',
                                workspace_paths={instance: destination_workspace})
        restored = Bridge(tmp_path / 'destination')
        assert restored.get_session(instance)['cwd'] == str(destination_workspace)
        stack.callback(restored.close, cancel=True)
        restored.checkpoints.register_content(checkpoint['content'],
                                              bridge.checkpoints.resolve_content(checkpoint['content']))
        for process in list(bridge._children.values()):
            process.wait(timeout=10)
        bridge.close()
        bridge.root.rename(tmp_path / 'origin-state-offline')
        workspace.rename(tmp_path / 'origin-workspace-offline')
        assert not bridge.root.exists() and not workspace.exists()
        try:
            restored.checkpoints.restore(format_version='1', operation_id=str(uuid4()), params={
                'checkpoint': checkpoint, 'destination_generation': binding['store_generation'],
                'content': checkpoint['content']})
        except BridgeError as error:
            with restored.store.connect() as db:
                operations = [row[0] for row in db.execute(
                    'SELECT state FROM checkpoint_restore_operations')]
            pytest.fail(f'Native restore failed safely: code={error.code}; '
                        f'retryable={error.retryable}; operations={operations}')
        restored.checkpoints.release_recovery(expected_generation=binding['store_generation'])
        env = {**base_environment(), 'FIXTURE_NATIVE_KEY': 'offline-client',
               'PYTHONPATH': str(Path(__file__).parents[1] / 'src')}
        listing, history = native_history(restored, instance, env)
        assert [item['id'] for item in listing['data']] == [checkpoint['native_id']]
        assert history['thread']['id'] == checkpoint['native_id']
        assert len(history['thread']['turns']) == 1
        next_turn = restored.message_create(instance, 'fixture request 2',
                                            permission_mode=permission_mode, timeout_ms=30000)['turn_id']
        assert restored.run(next_turn).wait(40)['state'] == 'completed'
        assert restored.get_session(instance)['native_id'] == checkpoint['native_id']
        assert observed[-1]['prompts'] == ['fixture request 1', 'fixture request 2']
        assert terminal(restored, next_turn).data['durability']['state'] == 'sealed'
