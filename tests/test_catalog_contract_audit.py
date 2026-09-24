"""Model discovery trusts only configured and verified local proxy evidence."""
import json

from types import SimpleNamespace

import pytest

from agentbridge import Bridge, BridgeError
from agentbridge import provider_catalog
from agentbridge.catalog import ModelCatalog
from fixtures.test_proxy_account_fixture import proxy_account, register_verified_proxy_account


def test_empty_proxy_catalog_does_not_invent_provider_models(tmp_path):
    with Bridge(tmp_path) as bridge:
        result = bridge.models()
        assert result['items'] == [] and result['models'] == []
        assert result['source'] == 'agentbridge_routing'
        assert result['stale'] is False


def test_engine_specific_catalog_requests_are_rejected(tmp_path):
    with Bridge(tmp_path) as bridge:
        for engine in ('codex', 'claude', 'grok'):
            with pytest.raises(BridgeError) as error:
                bridge.models(engine)
            assert error.value.code == 'unsupported_parameter'


@pytest.mark.parametrize('flag', ['refresh', 'include_hidden', 'include_deprecated'])
@pytest.mark.parametrize('value', ['false', 0, None])
def test_catalog_flags_require_booleans(tmp_path, flag, value):
    with Bridge(tmp_path) as bridge:
        with pytest.raises(BridgeError) as error:
            bridge.models(**{flag: value})
        assert error.value.code == 'invalid_input'


def test_verified_proxy_models_have_no_invented_metadata(tmp_path):
    with Bridge(tmp_path) as bridge:
        register_verified_proxy_account(bridge.store, 'codex-a', 11011,
                                        model='fixture-model', provider='codex')
        result = bridge.models()
        assert result['stale'] is False
        assert result['items'] == [{
            'id': 'fixture-model', 'display_name': 'fixture-model',
            'availability': 'proxy_observed', 'source': 'account_configuration',
            'candidate_account_refs': ['codex-a'], 'observed_account_refs': ['codex-a'],
            'providers': ['codex'], 'reasoning_efforts': [],
            'context_windows': [], 'input_modalities': [],
            'account_capabilities': [{
                'account_ref': 'codex-a', 'provider': 'codex', 'observed': True,
                'reasoning_efforts': [], 'default_reasoning_effort': None,
                'context_windows': [], 'input_modalities': [], 'metadata_source': None,
            }],
        }]


def test_unverified_proxy_observation_cannot_be_reported_as_live(tmp_path):
    with Bridge(tmp_path) as bridge:
        account = proxy_account('codex-a', 11011, model='fixture-model', provider='codex')
        with bridge.store.connect() as db:
            db.execute('INSERT INTO accounts(id,config) VALUES (?,?)',
                       (account.id, json.dumps(account.to_dict())))
        bridge.store.account_observation('codex-a', 'cliproxy_management', 'unknown', {
            'verified': False, 'reason': 'proxy_observation_unavailable',
            'models': [{'id': 'fixture-model'}],
        })
        result = bridge.models()
        assert result['stale'] is True
        assert result['items'][0]['availability'] == 'configured_unverified'
        assert result['items'][0]['observed_account_refs'] == []
        assert result['items'][0]['candidate_account_refs'] == ['codex-a']


def test_pagination_and_account_filter_use_proxy_route_catalog(tmp_path):
    with Bridge(tmp_path) as bridge:
        register_verified_proxy_account(bridge.store, 'codex-a', 11011,
                                        model='model-a', provider='codex')
        register_verified_proxy_account(bridge.store, 'claude-b', 11012,
                                        model='model-b', provider='claude')
        first = bridge.models(limit=1)
        assert [item['id'] for item in first['items']] == ['model-a']
        assert first['has_more'] and first['next_cursor'] == 1
        assert [item['id'] for item in bridge.models(cursor=first['next_cursor'])['items']] == ['model-b']
        selected = bridge.models(account_ref='claude-b')
        assert [item['id'] for item in selected['items']] == ['model-b']
        assert selected['items'][0]['providers'] == ['claude']
        filtered = bridge.models(provider='claude', limit=1)
        assert [item['id'] for item in filtered['items']] == ['model-b']
        assert filtered['next_cursor'] is None and not filtered['has_more']


def test_retired_direct_catalog_adapters_cannot_open_provider_credentials(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail('direct provider catalog was called')

    monkeypatch.setattr('subprocess.Popen', forbidden)
    for adapter in (lambda: provider_catalog.models(SimpleNamespace(engine='claude')),
                    lambda: ModelCatalog(None).list('codex', refresh=True)):
        with pytest.raises(BridgeError) as error:
            adapter()
        assert error.value.code == 'unsupported_operation'
