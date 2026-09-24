"""Context overrides use verified model ceilings before a turn is admitted."""

import pytest

from agentbridge import Bridge, BridgeError, RunOptions
from agentbridge.proxy.model_catalog import catalog_metadata
from agentbridge.routing.admission import prepare_turn
from fixtures.test_proxy_account_fixture import register_verified_proxy_account


MODEL = 'fixture-model'


def _controls(store, account_id, *, default=None, maximum=None):
    saved = store.latest_account_observation(account_id)
    metadata = catalog_metadata({'models': [{
        'slug': MODEL, 'context_window': default, 'max_context_window': maximum,
    }]})
    data = {**saved['data'], 'model_metadata': metadata,
            'model_metadata_source': 'cliproxy_client_models'}
    store.account_observation(account_id, 'cliproxy_management', 'active', data)


def _bridge(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / 'state')
    for account_id, port in (('codex-a', 11011), ('codex-b', 11012)):
        register_verified_proxy_account(bridge.store, account_id, port,
                                        model=MODEL, provider='codex')
    monkeypatch.setenv('FIXTURE_PROXY_KEY', 'fixture-client-key')
    refreshed = []

    def observation(account, *, refresh=False, now=None, include_catalog=True):
        refreshed.append((account.id, refresh))
        return bridge.store.latest_account_observation(account.id)

    monkeypatch.setattr(bridge.routes, 'observation', observation)
    return bridge, refreshed


def test_pinned_override_rejects_excess_and_unknown_before_admission(tmp_path, monkeypatch):
    bridge, refreshed = _bridge(tmp_path, monkeypatch)
    with bridge:
        workspace = tmp_path / 'workspace'
        workspace.mkdir()
        bridge.store.add_session('pinned', 'codex-a', str(workspace), MODEL)
        _controls(bridge.store, 'codex-a', default=131072, maximum=200000)
        with pytest.raises(BridgeError) as error:
            bridge.message_create('pinned', 'hello', context_window=200001)
        assert error.value.code == 'context_window_unavailable'
        assert error.value.safe_data()['category'] == 'limit'
        assert bridge.runs() == []

        _controls(bridge.store, 'codex-a', default=True, maximum='200000')
        with pytest.raises(BridgeError) as error:
            bridge.message_create('pinned', 'hello', context_window=131072)
        assert error.value.code == 'context_window_unavailable'
        assert bridge.runs() == []
        assert ('codex-a', True) in refreshed


def test_automatic_route_filters_insufficient_accounts_before_balancing(tmp_path, monkeypatch):
    bridge, refreshed = _bridge(tmp_path, monkeypatch)
    with bridge:
        workspace = tmp_path / 'workspace'
        workspace.mkdir()
        bridge.store.add_session('automatic', 'codex-a', str(workspace), MODEL,
                                 routing_mode='automatic')
        _controls(bridge.store, 'codex-a', default=65536, maximum=100000)
        _controls(bridge.store, 'codex-b', default=131072, maximum=200000)
        session = bridge.get_session('automatic')
        account, decision, *_ = prepare_turn(
            bridge, session, RunOptions(model=MODEL, context_window=150000))
        assert account.id == decision.account_id == 'codex-b'
        assert ('codex-a', True) in refreshed and ('codex-b', True) in refreshed
        assert bridge.runs() == []

        _controls(bridge.store, 'codex-b', default=False, maximum=None)
        with pytest.raises(BridgeError) as error:
            bridge.message_create('automatic', 'hello', context_window=150000)
        assert error.value.code == 'context_window_unavailable'
        assert bridge.runs() == []
