"""Manual policy changes preserve account ownership and explicit recovery."""

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.routing import RouteDecision
from agentbridge.routing.admission import prepare_turn
from fixtures.test_proxy_account_fixture import register_verified_proxy_account


MODEL = 'fixture-model'


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    value = Bridge(tmp_path / 'state')
    for name, port in [('a', 8301), ('b', 8302)]:
        register_verified_proxy_account(value.store, name, port, model=MODEL, provider='codex')
    monkeypatch.setenv('FIXTURE_PROXY_KEY', 'fixture-value')
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', 'fixture-management-value')
    monkeypatch.setattr(value.routes, 'observation',
                        lambda account, **kwargs: value.store.latest_account_observation(account.id))
    return value


def create(bridge, tmp_path, **kwargs):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    return bridge.instance_create(model=MODEL, workspace_path=workspace, **kwargs)


def completed(bridge, instance_id, account_id='a'):
    automatic = bridge.store.routing(instance_id)['mode'] == 'automatic'
    decision = RouteDecision(account_id, MODEL, 'unknown', None, 'healthy', 0, 'affinity')
    bridge.store.admit('first', instance_id, 'Original user request', RunOptions(), None,
                       account_id=account_id, route_decision=decision if automatic else None)
    bridge.store.emit('first', 'session', {'native_id': f'native-{account_id}'})
    bridge.store.emit('first', 'assistant', {'text': 'Original response'})
    bridge.store.finish('first', 'completed')


def test_explicit_initial_automatic_account_is_durable_and_idempotent(bridge, tmp_path):
    instance = create(bridge, tmp_path, routing_mode='automatic', account_ref='b',
                      provider='codex', idempotency_key='create-key')
    assert instance['routing_mode'] == 'automatic'
    assert instance['affinity_account_ref'] == 'b'
    assert instance['account_ref'] == 'b'
    replay = create(bridge, tmp_path, routing_mode='automatic', account_ref='b',
                    provider='codex', idempotency_key='create-key')
    assert replay['replayed'] and replay['id'] == instance['id']
    for kwargs in ({'account_ref': 'a'}, {}, {'account_ref': 'b', 'routing_mode': 'pinned'}):
        with pytest.raises(BridgeError) as error:
            create(bridge, tmp_path, idempotency_key='create-key', **kwargs)
        assert error.value.code == 'idempotency_conflict'


def test_pinned_default_and_mode_toggles_keep_native_thread(bridge, tmp_path):
    instance = create(bridge, tmp_path, account_ref='a')
    completed(bridge, instance['id'])
    automatic = bridge.instance_update(instance['id'], routing_mode='automatic', expected_version=1)
    assert automatic['affinity_account_ref'] == 'a'
    assert automatic['native_session_id'] == 'native-a'
    pinned = bridge.instance_update(instance['id'], routing_mode='pinned', expected_version=2)
    assert pinned['routing_mode'] == 'pinned'
    assert pinned['affinity_account_ref'] is None
    assert pinned['account_ref'] == 'a' and pinned['native_session_id'] == 'native-a'


@pytest.mark.parametrize('mode', ['automatic', 'pinned'])
def test_manual_account_change_seeds_bounded_context_without_foreign_native(bridge, tmp_path, mode):
    instance = create(bridge, tmp_path, account_ref='a')
    completed(bridge, instance['id'])
    updated = bridge.instance_update(instance['id'], account_ref='b', routing_mode=mode,
                                     expected_version=1)
    assert updated['account_ref'] == 'b'
    assert updated['native_session_id'] is None
    assert bridge.store.routing(instance['id'])['last_completed_account_id'] == 'a'
    account, decision, context, omissions, snapshot = prepare_turn(
        bridge, bridge.get_session(instance['id']), RunOptions(model=MODEL))
    assert account.id == 'b'
    assert 'Original user request' in context and 'Original response' in context
    bridge.store.admit('next', instance['id'], 'Next request', RunOptions(model=MODEL), None,
                       account_id=account.id, route_decision=decision, route_context=context,
                       route_omissions=omissions, route_event_seq=snapshot)
    assert bridge.get_session(instance['id'])['native_id'] is None
    assert bridge.get_session(instance['id'])['context'] == context


def test_account_only_update_pins_and_checks_model_atomically(bridge, tmp_path):
    instance = create(bridge, tmp_path, account_ref='a', routing_mode='automatic')
    updated = bridge.instance_update(instance['id'], account_ref='b', expected_version=1)
    assert updated['routing_mode'] == 'pinned' and updated['account_ref'] == 'b'
    with pytest.raises(BridgeError) as error:
        bridge.instance_update(instance['id'], account_ref='a', model='unsupported',
                               expected_version=2)
    assert error.value.code == 'model_unavailable'
    assert bridge.instance_get(instance['id'])['account_ref'] == 'b'
    assert bridge.instance_get(instance['id'])['version'] == 2


@pytest.mark.parametrize('mode', [None, 'invalid', [], 0])
def test_invalid_update_mode_is_rejected(bridge, tmp_path, mode):
    instance = create(bridge, tmp_path, account_ref='a')
    with pytest.raises(BridgeError) as error:
        bridge.instance_update(instance['id'], routing_mode=mode)
    assert error.value.code == 'invalid_routing_mode'
    assert bridge.instance_get(instance['id'])['version'] == 1


def test_pinned_requires_account_and_initial_account_must_be_eligible(bridge, tmp_path):
    for kwargs, code in [
        ({'routing_mode': 'pinned'}, 'account_ref_required'),
        ({'routing_mode': 'automatic', 'account_ref': 'a', 'provider': 'claude'}, 'provider_unavailable'),
        ({'routing_mode': 'automatic', 'account_ref': 'a', 'excluded_account_refs': ['a']}, 'invalid_request'),
    ]:
        with pytest.raises(BridgeError) as error:
            create(bridge, tmp_path, **kwargs)
        assert error.value.code == code
    bridge.store.set_account_paused('a', True)
    with pytest.raises(BridgeError) as error:
        create(bridge, tmp_path, routing_mode='automatic', account_ref='a')
    assert error.value.code == 'account_paused'


def test_failed_new_affinity_is_visible_without_owning_old_native(bridge, tmp_path):
    instance = create(bridge, tmp_path, account_ref='a', routing_mode='automatic')
    completed(bridge, instance['id'])
    bridge.store.admit('failed-b', instance['id'], 'Next request', RunOptions(model=MODEL), None,
                       account_id='b', route_decision=RouteDecision(
                           'b', MODEL, 'unknown', None, 'healthy', 0, 'quota_unknown'),
                       route_context='Bounded previous evidence',
                       route_event_seq=bridge.store.last_route_event_seq(instance['id']))
    bridge.store.finish('failed-b', 'failed', 'provider_failed')
    value = bridge.instance_get(instance['id'])
    assert value['affinity_account_ref'] == 'b'
    assert value['account_ref'] == 'a' and value['native_session_id'] == 'native-a'
    pinned = bridge.instance_update(instance['id'], routing_mode='pinned')
    assert pinned['account_ref'] == 'b' and pinned['native_session_id'] is None
    assert bridge.store.session_run_count(instance['id']) == 2
