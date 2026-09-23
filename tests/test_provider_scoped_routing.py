"""A selected provider constrains every automatic route for a conversation."""

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.accounts import AccountService
from agentbridge.errors import BridgeError
from agentbridge.routing import RouteDecision
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
    bridge = Bridge(tmp_path / 'state')
    _accounts(bridge.store)
    register_verified_proxy_account(bridge.store, 'codex-exclusive', 8303,
                                    model='codex-only-model', provider='codex')
    decision = RouteDecision('anthropic', MODEL, 'unknown', None, 'healthy', 0,
                             'quota_unknown')
    selected = []

    def select(model, *, provider=None, refresh=True):
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
