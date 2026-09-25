"""A selected provider constrains every automatic route for a conversation."""

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.accounts import AccountService
from agentbridge.errors import BridgeError
from agentbridge.routing import RouteDecision
from agentbridge.routing.admission import prepare_turn
from agentbridge.routing.service import RoutingService
from agentbridge.store import Store
from fixtures.test_proxy_account_fixture import register_verified_proxy_account


MODEL = 'shared-fixture-model'


def _accounts(store):
    register_verified_proxy_account(store, 'openai', 8301, model=MODEL, provider='codex')
    register_verified_proxy_account(store, 'anthropic', 8302, model=MODEL, provider='claude')


def test_provider_filters_route_candidates_before_selection(tmp_path, monkeypatch):
    store = Store(tmp_path / 'state')
    _accounts(store)
    monkeypatch.setenv('FIXTURE_PROXY_KEY', 'local-fixture-value')
    routes = RoutingService(store, AccountService(store))

    assert [item.account_id for item in routes.candidates(MODEL, provider='claude')] == ['anthropic']
    assert routes.select(MODEL, provider='claude', refresh=False).account_id == 'anthropic'
    with pytest.raises(BridgeError) as caught:
        routes.select(MODEL, provider='xai', refresh=False)
    assert caught.value.code == 'model_unavailable'


def test_provider_constraint_survives_replay_and_is_enforced_at_admission(tmp_path):
    store = Store(tmp_path / 'state')
    _accounts(store)
    assert store.add_session('conversation', 'anthropic', str(tmp_path), MODEL,
                             request_key='scoped-key', routing_mode='automatic',
                             routing_provider='claude') == ('conversation', True)
    assert store.routing('conversation')['provider'] == 'claude'
    assert store.replay_auto_session('scoped-key', str(tmp_path), MODEL,
                                     provider='claude') == 'conversation'
    with pytest.raises(BridgeError) as caught:
        store.replay_auto_session('scoped-key', str(tmp_path), MODEL, provider='codex')
    assert caught.value.code == 'idempotency_conflict'

    wrong = RouteDecision('openai', MODEL, 'unknown', None, 'healthy', 0, 'quota_unknown')
    with pytest.raises(BridgeError) as caught:
        store.admit('wrong', 'conversation', 'prompt', RunOptions(model=MODEL), None,
                    account_id='openai', route_decision=wrong)
    assert caught.value.code == 'provider_unavailable'
    assert store.list('runs') == []


def test_public_instance_creation_keeps_provider_filter(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path.parent / f'{tmp_path.name}-state')
    _accounts(bridge.store)
    register_verified_proxy_account(bridge.store, 'codex-exclusive', 8303,
                                    model='codex-only-model', provider='codex')
    decision = RouteDecision('anthropic', MODEL, 'unknown', None, 'healthy', 0,
                             'quota_unknown')
    selected = []

    def select(model, *, provider=None, refresh=True, excluded_account_refs=()):
        selected.append((model, provider))
        return decision

    monkeypatch.setattr(bridge.routes, 'select', select)
    instance = bridge.instance_create(model=MODEL, provider='claude',
                                      workspace_path=tmp_path)
    assert instance['routing_mode'] == 'automatic'
    assert instance['routing_provider'] == 'claude'
    assert instance['account_ref'] == 'anthropic'
    assert bridge.store.routing(instance['id'])['provider'] == 'claude'
    assert selected == [(MODEL, 'claude')]

    with pytest.raises(BridgeError) as caught:
        bridge.instance_update(instance['id'], model='codex-only-model')
    assert caught.value.code == 'model_unavailable'
    assert bridge.instance_get(instance['id'])['model'] == MODEL

    with pytest.raises(BridgeError) as caught:
        bridge.instance_create(model=MODEL, provider='codex', account_ref='openai',
                               workspace_path=tmp_path)
    assert caught.value.code == 'unsupported_parameter'


def test_model_and_provider_change_together_for_disjoint_catalogs(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    register_verified_proxy_account(bridge.store, 'openai', 8301,
                                    model='codex-only', provider='codex')
    register_verified_proxy_account(bridge.store, 'anthropic', 8302,
                                    model='claude-only', provider='claude')
    bridge.store.add_session('conversation', 'openai', str(tmp_path), 'codex-only',
                             routing_mode='automatic', routing_provider='codex')

    updated = bridge.instance_update('conversation', model='claude-only',
                                     provider='claude', expected_version=1)
    assert (updated['model'], updated['routing_mode'], updated['routing_provider'],
            updated['version']) == ('claude-only', 'automatic', 'claude', 2)
    assert bridge.store.routing('conversation')['provider'] == 'claude'

    with pytest.raises(BridgeError) as caught:
        bridge.instance_update('conversation', model='codex-only', expected_version=2)
    assert caught.value.code == 'model_unavailable'
    assert bridge.instance_get('conversation')['model'] == 'claude-only'
    assert bridge.instance_get('conversation')['version'] == 2


def test_provider_clear_and_failed_changes_are_atomic(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    register_verified_proxy_account(bridge.store, 'openai', 8301,
                                    model='codex-only', provider='codex')
    register_verified_proxy_account(bridge.store, 'anthropic', 8302,
                                    model='claude-only', provider='claude')
    bridge.store.add_session('conversation', 'openai', str(tmp_path), 'codex-only',
                             routing_mode='automatic', routing_provider='codex')

    for changes, code in (
        ({'provider': 'claude'}, 'model_unavailable'),
        ({'provider': 'grok', 'model': 'claude-only'}, 'model_unavailable'),
        ({'provider': 'xai', 'model': 'claude-only'}, 'invalid_provider'),
        ({'provider': [], 'model': 'claude-only'}, 'invalid_provider'),
        ({'provider': 'claude', 'model': 'claude-only', 'expected_version': 2},
         'version_conflict'),
    ):
        with pytest.raises(BridgeError) as caught:
            bridge.instance_update('conversation', **changes)
        assert caught.value.code == code
        assert bridge.instance_get('conversation')['model'] == 'codex-only'
        assert bridge.instance_get('conversation')['routing_provider'] == 'codex'
        assert bridge.instance_get('conversation')['version'] == 1

    cleared = bridge.instance_update('conversation', provider=None, expected_version=1)
    assert cleared['routing_provider'] is None
    assert cleared['model'] == 'codex-only'
    assert cleared['version'] == 2
    selected = bridge.instance_update('conversation', model='claude-only', expected_version=2)
    assert selected['routing_provider'] is None
    assert selected['model'] == 'claude-only'


def test_route_change_refuses_active_archived_and_evaluation_instances(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    _accounts(bridge.store)
    bridge.store.add_session('active', 'openai', str(tmp_path), MODEL,
                             routing_mode='automatic', routing_provider='codex')
    bridge.store.admit('running-turn', 'active', 'hello', RunOptions(model=MODEL), None,
                       account_id='openai', route_decision=RouteDecision(
                           'openai', MODEL, 'unknown', None, 'healthy', 0, 'quota_unknown'))
    with pytest.raises(BridgeError) as caught:
        bridge.instance_update('active', provider='claude', expected_version=1)
    assert caught.value.code == 'busy'
    assert bridge.instance_get('active')['routing_provider'] == 'codex'
    assert bridge.instance_get('active')['version'] == 1
    bridge.store.finish('running-turn', 'completed')

    bridge.instance_archive('active', expected_version=1)
    with pytest.raises(BridgeError) as caught:
        bridge.instance_update('active', provider='claude', expected_version=2)
    assert caught.value.code == 'instance_archived'

    bridge.store.add_session('evaluation', 'openai', str(tmp_path), MODEL,
                             routing_mode='automatic', routing_provider='codex',
                             evaluation=True)
    with pytest.raises(BridgeError) as caught:
        bridge.instance_update('evaluation', provider='claude', expected_version=1)
    assert caught.value.code == 'evaluation_immutable'
    assert bridge.instance_get('evaluation')['routing_provider'] == 'codex'


def test_pinned_chat_can_switch_provider_with_same_native_thread(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / 'state')
    _accounts(bridge.store)
    bridge.store.add_session('conversation', 'openai', str(tmp_path), MODEL)
    bridge.store.admit('first', 'conversation', 'prior user request', RunOptions(), None)
    bridge.store.emit('first', 'session', {'native_id': 'native-openai'})
    bridge.store.finish('first', 'completed')
    assert bridge.store.routing('conversation')['last_native_id'] == 'native-openai'

    updated = bridge.instance_update('conversation', provider='claude', expected_version=1)
    assert updated['routing_mode'] == 'automatic'
    assert updated['routing_provider'] == 'claude'
    assert bridge.store.session_run_count('conversation') == 1
    assert bridge.store.routing('conversation')['last_completed_account_id'] == 'openai'
    monkeypatch.setattr(bridge.routes, 'select', lambda *args, **kwargs:
                        RouteDecision('anthropic', MODEL, 'unknown', None,
                                      'healthy', 0, 'quota_unknown'))
    account, decision, context, omissions, snapshot = prepare_turn(
        bridge, bridge.get_session('conversation'), RunOptions(model=MODEL))
    assert account.id == 'anthropic'
    assert context is None and omissions == 0
    bridge.store.admit('next', 'conversation', 'new request', RunOptions(model=MODEL), None,
                       account_id=account.id, route_decision=decision,
                       route_context=context, route_omissions=omissions,
                       route_event_seq=snapshot)
    route = [event.data for event in bridge.store.events(run_id='next')
             if event.kind == 'route_selected'][0]
    assert route['account_changed'] is True
    assert route['portable_context_used'] is False
    assert route['previous_account_id'] == 'openai'
    assert bridge.instance_get('conversation')['native_session_id'] == 'native-openai'


@pytest.mark.parametrize('provider', [None, 'codex'])
def test_explicit_provider_converts_pinned_instance(tmp_path, provider):
    bridge = Bridge(tmp_path / 'state')
    _accounts(bridge.store)
    bridge.store.add_session('conversation', 'openai', str(tmp_path), MODEL)
    updated = bridge.instance_update('conversation', provider=provider, expected_version=1)
    assert updated['routing_mode'] == 'automatic'
    assert updated['routing_provider'] == provider
    assert updated['account_ref'] == 'openai'


def test_retired_account_does_not_make_provider_model_eligible(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    _accounts(bridge.store)
    bridge.store.add_session('conversation', 'openai', str(tmp_path), MODEL,
                             routing_mode='automatic', routing_provider='codex')
    bridge.store.retire_account('anthropic')
    with pytest.raises(BridgeError) as caught:
        bridge.instance_update('conversation', provider='claude', expected_version=1)
    assert caught.value.code == 'model_unavailable'
    assert bridge.instance_get('conversation')['routing_provider'] == 'codex'
