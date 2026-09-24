"""Release changes never silently inherit compatibility or rewrite history."""
import copy
import json
from pathlib import Path
import sys

import pytest

from agentbridge import Account, Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.provider_contracts import ContractRegistry, digest, index
from agentbridge.rpc import dispatch
from fixtures.test_proxy_account_fixture import (
    register_verified_proxy_account, seed_authenticated_proxy_account)
from test_proxy_binding import management


@pytest.fixture
def registry(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path.parent / f'{tmp_path.name}-state')
    register_verified_proxy_account(bridge.store, 'a', 8317, provider='codex')
    monkeypatch.setattr('agentbridge.error_observer.provider_version',
                        lambda account, state_root=None: '0.153.0')
    return ContractRegistry(bridge.store), bridge


def observation(registry, **changes):
    binding = registry.list('codex')['bindings'][0]
    return {'component': binding['component'], 'version': binding['version'],
            'evidence_kind': binding['evidence_kind'], 'structural_hash': binding['structural_hash'],
            'surface_names': ['fixture'], 'limitations': [], **changes}


def inspect_value(monkeypatch, registry, **changes):
    monkeypatch.setattr('agentbridge.provider_fingerprint.inspect_surface',
                        lambda engine, state_root=None: observation(registry, **changes))
    return registry.inspect('codex')


def test_index_shared_contracts_have_stable_content_hashes():
    data = index()
    assert len(data['contracts']) == len(data['bindings']) == 2
    for profile in data['contracts']:
        assert profile['id'] == 'sha256:' + digest(profile['manifest'])
        assert digest(profile['manifest']) == digest(dict(reversed(list(profile['manifest'].items()))))


def test_unknown_release_blocked_before_credentials_admission_or_spawn(registry, monkeypatch, tmp_path):
    registry, bridge = registry
    monkeypatch.setattr('agentbridge.error_observer.provider_version',
                        lambda account, state_root=None: '0.154.0')
    def unexpected(account):
        pytest.fail('Credentials must not be resolved for unreviewed releases')
    monkeypatch.setattr('agentbridge.routing.execution.environment', unexpected)
    monkeypatch.setattr('agentbridge.routing.admission.verify_proxy_model', lambda *args, **kwargs: None)
    session_id, _ = bridge.store.add_session('fixture-session', 'a', str(tmp_path), 'fixture-model')
    with pytest.raises(BridgeError) as failure:
        bridge.submit(session_id, 'hello')
    assert failure.value.code == 'provider_contract_unverified'
    assert failure.value.outcome == 'not_started'
    assert bridge.runs() == []


def test_same_structure_new_release_suggests_reuse_without_activation(registry, monkeypatch):
    registry, bridge = registry
    result = inspect_value(monkeypatch, registry, version='0.154.0')
    assert result['status'] == 'candidate' and result['suggested_contract_id']
    monkeypatch.setattr('agentbridge.error_observer.provider_version',
                        lambda account, state_root=None: '0.154.0')
    assert not registry.check(bridge.account('a'))['native_operations_allowed']
    again = ContractRegistry(bridge.store).latest('codex', '0.154.0')
    assert again == result


def test_drift_survives_insufficient_observation_and_restart(registry, monkeypatch):
    registry, bridge = registry
    drift = inspect_value(monkeypatch, registry, structural_hash='0' * 64)
    assert drift['status'] == 'drift_detected'
    inspect_value(monkeypatch, registry, structural_hash=None)
    restarted = ContractRegistry(Bridge(bridge.root).store)
    assert restarted.check(bridge.account('a'))['status'] == 'drift_detected'
    fixed = inspect_value(monkeypatch, registry)
    assert fixed['status'] == 'unchanged'
    assert restarted.check(bridge.account('a'))['native_operations_allowed']


def test_changed_structure_does_not_suggest_reuse(registry, monkeypatch):
    registry, bridge = registry
    result = inspect_value(monkeypatch, registry, version='0.154.0', structural_hash='1' * 64)
    assert result['status'] == 'candidate' and result['suggested_contract_id'] is None


def test_worker_version_change_and_old_receipt_are_observable(registry, monkeypatch):
    registry, bridge = registry
    saved = registry.record_run('fixture-run', registry.check(bridge.account('a')))
    monkeypatch.setattr('agentbridge.error_observer.provider_version',
                        lambda account, state_root=None: '0.154.0')
    with pytest.raises(BridgeError, match='review'):
        registry.verify_run(bridge.account('a'), 'fixture-run')
    assert ContractRegistry(bridge.store).run('fixture-run') == saved
    registry.record_run('fixture-run', {'version': 'invented'})
    assert registry.run('fixture-run') == saved


def test_legacy_run_does_not_invent_admission_version(registry):
    registry, bridge = registry
    registry.verify_run(bridge.account('a'), 'legacy')
    assert registry.run('legacy')['admission_version_unobserved'] is True


def test_custom_launcher_is_explicitly_unverified(registry):
    registry, bridge = registry
    account = Account('custom', 'codex', command=('local-wrapper',), provider='codex',
                      supported_models=('fixture-model',),
                      proxy_base_url='http://127.0.0.1:8318/v1', key_env='FIXTURE_PROXY_KEY',
                      management_key_env='FIXTURE_MANAGEMENT_KEY')
    result = registry.check(account, enforce=True)
    assert result['status'] == 'custom_adapter' and result['verification'] == 'unverified'


def test_rpc_catalog_is_not_live_acceptance(registry):
    registry, bridge = registry
    value = dispatch(bridge, 'contracts.list', {'engine': 'codex'})
    profile = value['contracts'][0]
    assert dispatch(bridge, 'contracts.get', {'contract_id': profile['id']}) == profile
    assert dispatch(bridge, 'contracts.check', {'account_ref': 'a'})['status'] == 'reviewed'


@pytest.mark.parametrize('mutation', ['missing', 'cross_engine', 'duplicate', 'bad_hash'])
def test_corrupt_index_is_rejected(monkeypatch, mutation):
    data = copy.deepcopy(index())
    if mutation == 'missing':
        data['bindings'][0]['contract_id'] = 'missing'
    elif mutation == 'cross_engine':
        data['bindings'][0]['contract_id'] = data['contracts'][1]['id']
    elif mutation == 'duplicate':
        data['bindings'].append(data['bindings'][0])
    else:
        data['contracts'][0]['manifest']['engine'] = 'invented'
    class Resource:
        def joinpath(self, name):
            return self
        def read_text(self):
            return json.dumps(data)
    monkeypatch.setattr('agentbridge.provider_contracts.files', lambda name: Resource())
    with pytest.raises(BridgeError) as failure:
        index()
    assert failure.value.code == 'provider_contract_invalid'


def test_completed_idempotent_replay_survives_upgrade(registry, monkeypatch, tmp_path):
    registry, bridge = registry
    session_id, _ = bridge.store.add_session('fixture-session', 'a', str(tmp_path), 'fixture-model')
    run_id, created = bridge.store.admit('existing', session_id, 'hello', RunOptions(), 'same-key')
    receipt = registry.record_run(run_id, registry.check(bridge.account('a')))
    bridge.store.finish(run_id, 'completed')
    monkeypatch.setattr('agentbridge.error_observer.provider_version',
                        lambda account, state_root=None: '0.154.0')
    replay = bridge.submit(session_id, 'hello', request_key='same-key')
    assert replay.replayed and replay.id == run_id
    assert bridge.turn(run_id)['provider_compatibility'] == receipt


def test_inspection_detected_component_mismatch_blocks_later_calls(registry, monkeypatch):
    registry, bridge = registry
    def mismatch(engine, state_root=None):
        raise BridgeError('provider_contract_changed', 'Components differ.',
                          details={'engine': 'codex', 'version': '0.153.0', 'component': 'codex-cli'})
    monkeypatch.setattr('agentbridge.provider_fingerprint.inspect_surface', mismatch)
    assert registry.inspect('codex')['status'] == 'drift_detected'
    assert not registry.check(bridge.account('a'))['native_operations_allowed']


def test_unindexed_release_keeps_proxy_metadata_separate_from_native_probes(registry, monkeypatch):
    registry, bridge = registry
    monkeypatch.setattr('agentbridge.error_observer.provider_version',
                        lambda account, state_root=None: '0.154.0')
    models = bridge.models(account_ref='a')
    assert models['models'][0]['availability'] == 'proxy_observed'
    account = bridge.account_status('a')
    assert account['authentication']['source'] == 'cliproxy_management'
    assert account['binding_verified'] is True
    usage = bridge.account_usage('a')
    assert usage['stale'] and not usage['supported']
    assert registry.check(bridge.account('a'))['status'] == 'unindexed_version'


def test_persisted_proxy_observation_survives_restart_without_a_native_probe(registry, monkeypatch):
    registry, bridge = registry
    refreshed = bridge.account_status('a')
    cached = Bridge(bridge.root).account_status('a')
    assert refreshed == cached
    assert cached['authentication']['source'] == 'cliproxy_management'
    assert registry.check(bridge.account('a'))['version'] == '0.153.0'


def test_project_cannot_shadow_internal_worker_import(tmp_path, monkeypatch):
    workspace = tmp_path / 'project'
    shadow = workspace / 'agentbridge'
    shadow.mkdir(parents=True)
    (shadow / '__init__.py').write_text("raise RuntimeError('project shadow imported')\n")
    fixture = Path(__file__).resolve().parents[1] / 'examples/fake_provider.py'
    monkeypatch.setenv('FIXTURE_PROXY_KEY', 'fixture-only-client-key')
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', 'fixture-only-management-key')
    with management('fixture-private-account', 'fixture-private-index') as (port, _):
        bridge = Bridge(tmp_path / 'state')
        seed_authenticated_proxy_account(bridge.store, Account('fixture', 'codex', command=('/usr/bin/python3', '-c', fixture.read_text()),
            provider='codex', supported_models=('gpt-5',),
            proxy_base_url=f'http://127.0.0.1:{port}/v1', key_env='FIXTURE_PROXY_KEY',
            management_key_env='FIXTURE_MANAGEMENT_KEY'), observe_local=True)
        session = bridge.session('fixture', workspace, model='gpt-5')
        monkeypatch.chdir(workspace)
        try:
            run = bridge.submit(session['id'], 'fixture')
            assert run.wait(10)['state'] == 'completed'
            assert bridge.turn(run.id)['provider_compatibility']['status'] == 'custom_adapter'
        finally:
            bridge.close(cancel=True)
