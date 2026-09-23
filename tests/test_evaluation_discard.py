"""Disposable evaluation instances cannot outlive or replay their evidence."""

from dataclasses import replace
import json
import time

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.error_observer import ErrorObserver
from agentbridge.provider_errors import normalize
from agentbridge.rpc import dispatch
from fixtures.test_proxy_account_fixture import proxy_account, seed_authenticated_proxy_account
from test_interactive_inputs import FIXTURE, context_package, management_server


@pytest.fixture
def bridge_for_evaluation(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_PROXY_KEY', 'fixture-client-key')
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', 'fixture-management-key')
    with management_server() as port:
        bridge = Bridge(tmp_path / 'state')
        account = replace(proxy_account('test', port, provider='codex'),
                          command=('/usr/bin/python3', '-c', FIXTURE.read_text()))
        account = seed_authenticated_proxy_account(bridge.store, account,
                                                   observe_local=True)
        assert bridge.routes.observe(account)['status'] == 'active'
        yield bridge
        bridge.close(cancel=True)


def create(bridge, workspace, *, evaluation=True, key=None):
    if evaluation:
        workspace = workspace / 'empty-evaluation-workspace'
        workspace.mkdir(exist_ok=True)
    return bridge.instance_create(account_ref='test', model='fixture-model',
                                  workspace_path=str(workspace), evaluation=evaluation,
                                  idempotency_key=key)


def evaluation_package():
    package = context_package()
    package['execution_mode'] = 'evaluation_inputs_only'
    return package


def wait_for_discard(bridge, instance_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        receipt = dispatch(bridge, 'instances.discard_evaluation',
                           {'instance_id': instance_id, 'account_ref': 'test'})
        if receipt['discarded']:
            return receipt
        assert receipt['pending']
        time.sleep(.02)
    raise AssertionError('Evaluation processes did not terminate before discard.')


def test_discard_requires_an_explicit_evaluation_instance(bridge_for_evaluation, tmp_path):
    bridge = bridge_for_evaluation
    ordinary = create(bridge, tmp_path, evaluation=False)
    assert ordinary['evaluation'] is False
    with pytest.raises(BridgeError) as error:
        bridge.instance_discard_evaluation(ordinary['id'])
    assert error.value.code == 'evaluation_required'
    with pytest.raises(BridgeError) as error:
        bridge.instance_create(account_ref='test', model='fixture-model',
                               workspace_path=str(tmp_path), evaluation=1)
    assert error.value.code == 'invalid_request'
    assert bridge.instance_get(ordinary['id'])['state'] == 'active'
    assert bridge.capabilities()['operations']['instances.discard_evaluation']['maturity'] == 'fixture_tested'


def test_evaluation_requires_context_and_accepts_only_one_turn(bridge_for_evaluation, tmp_path):
    bridge = bridge_for_evaluation
    instance = create(bridge, tmp_path, key='eval-create')
    assert instance['evaluation'] is True
    assert create(bridge, tmp_path, key='eval-create')['replayed'] is True
    with pytest.raises(BridgeError) as error:
        bridge.message_create(instance['id'], 'no context')
    assert error.value.code == 'evaluation_context_required'
    normal_package = context_package()
    with pytest.raises(BridgeError) as error:
        bridge.message_create(instance['id'], 'normal context', context_package=normal_package)
    assert error.value.code == 'evaluation_context_required'
    package = evaluation_package()
    accepted = bridge.message_create(instance['id'], 'inspect-context',
                                     context_package=package, idempotency_key='eval-turn')
    assert bridge.run(accepted['turn_id']).wait(10)['state'] == 'completed'
    replay = bridge.message_create(instance['id'], 'inspect-context',
                                   context_package=package, idempotency_key='eval-turn')
    assert replay['replayed'] is True and replay['turn_id'] == accepted['turn_id']
    with pytest.raises(BridgeError) as error:
        bridge.message_create(instance['id'], 'another turn', context_package=package)
    assert error.value.code == 'evaluation_consumed'
    with pytest.raises(BridgeError) as error:
        bridge.transfer(instance['id'], 'test', validate_only=True)
    assert error.value.code == 'evaluation_instance'


def test_discard_purges_turn_and_native_home_without_replay(bridge_for_evaluation, tmp_path):
    bridge = bridge_for_evaluation
    instance = create(bridge, tmp_path, key='discard-create')
    prompt = 'private-evaluation-prompt-73129'
    accepted = bridge.message_create(instance['id'], prompt,
                                     context_package=evaluation_package(),
                                     idempotency_key='discard-turn')
    assert bridge.run(accepted['turn_id']).wait(10)['state'] == 'completed'
    home = bridge.root / 'codex-runtime' / instance['id']
    assert home.is_dir()
    receipt = wait_for_discard(bridge, instance['id'])
    assert receipt == {'instance_id': instance['id'], 'discarded': True, 'pending': False}
    assert not home.exists()
    assert bridge.instance_discard_evaluation(instance['id']) == receipt
    assert bridge.runs() == [] and bridge.sessions() == []
    with bridge.store.connect() as db:
        assert db.execute('SELECT count(*) FROM events WHERE session_id=?',
                          (instance['id'],)).fetchone()[0] == 0
        marker = db.execute('SELECT * FROM evaluation_instances WHERE session_id=?',
                            (instance['id'],)).fetchone()
        assert marker['status'] == 'discarded' and marker['discarded_at'] is not None
        request = db.execute('SELECT payload FROM instance_requests WHERE session_id=?',
                             (instance['id'],)).fetchone()
        assert json.loads(request['payload']) == {'evaluation_discarded': True}
    assert prompt.encode() not in bridge.store.path.read_bytes()
    with pytest.raises(BridgeError) as error:
        create(bridge, tmp_path, key='discard-create')
    assert error.value.code == 'evaluation_discarded'
    with pytest.raises(BridgeError) as error:
        bridge.instance_get(instance['id'])
    assert error.value.code == 'not_found'


def test_discard_waits_for_active_or_unidentified_process(bridge_for_evaluation, tmp_path):
    bridge = bridge_for_evaluation
    instance = create(bridge, tmp_path)
    bridge.store.admit('eval-run', instance['id'], 'private prompt', RunOptions(), None)
    pending = bridge.instance_discard_evaluation(instance['id'])
    assert pending == {'instance_id': instance['id'], 'discarded': False, 'pending': True}
    assert bridge.instance_get(instance['id'])['evaluation'] is True
    bridge.store.finish('eval-run', 'interrupted', 'worker_lost')
    bridge.store.update('eval-run', child_pid=12345, child_identity=None)
    assert bridge.instance_discard_evaluation(instance['id'])['pending'] is True
    bridge.store.update('eval-run', child_pid=None, child_identity=None)
    assert bridge.instance_discard_evaluation(instance['id'])['discarded'] is True


def test_discard_rejects_symlink_home_and_can_retry(bridge_for_evaluation, tmp_path):
    bridge = bridge_for_evaluation
    instance = create(bridge, tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'keep.txt'
    sentinel.write_text('keep')
    runtime = bridge.root / 'codex-runtime'
    runtime.mkdir()
    link = runtime / instance['id']
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(BridgeError) as error:
        bridge.instance_discard_evaluation(instance['id'])
    assert error.value.code == 'unsafe_store'
    assert sentinel.read_text() == 'keep'
    link.unlink()
    assert bridge.instance_discard_evaluation(instance['id'])['discarded'] is True


def test_evaluation_errors_do_not_enter_shared_learning(bridge_for_evaluation, tmp_path):
    bridge = bridge_for_evaluation
    instance = create(bridge, tmp_path)
    bridge.store.admit('eval-error', instance['id'], 'private prompt', RunOptions(), None)
    issue = normalize('codex', {'code': 'unrecognized-fixture-error'})
    assert ErrorObserver(bridge.store, bridge.account('test'), 'eval-error')(issue) == issue
    with bridge.store.connect() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='error_cases'").fetchone()
        assert exists is None
