"""Published metadata and unavailable discovery must not become invented facts."""
from types import SimpleNamespace

import pytest

from agentbridge import Account, Bridge, BridgeError
from agentbridge.account_probe import CodexAppServerProbe, stamp
from agentbridge.catalog import _model
from agentbridge.cursor_options import model_selection
from agentbridge import claude_account, provider_catalog


def test_unobserved_catalog_has_no_hardcoded_models_or_validity(tmp_path):
    with Bridge(tmp_path) as bridge:
        for engine in ('codex', 'claude', 'cursor'):
            result = bridge.models(engine)
            assert result['items'] == [] and result['models'] == []
            assert result['supported'] is False and result['stale'] is True
            assert result['source'] == 'static'  # Legacy wire label, never provider evidence.
            assert result['reason'] == 'live_catalog_requires_account'


def test_missing_flags_and_malformed_metadata_remain_unknown():
    value = _model({'id': 'future-model', 'isDefault': 'false', 'deprecated': 0,
                    'toolSupport': {}, 'subagentSupport': 'true', 'displayName': False,
                    'defaultReasoningEffort': ['high'], 'variants': [{}], 'serviceTiers': 'fast'})
    assert value['is_default'] is value['deprecated'] is None
    assert value['tool_support'] is value['subagent_support'] is None
    assert value['display_name'] == 'future-model' and value['default_reasoning_effort'] is None
    assert value['variants'][0]['is_default'] is None and value['service_tiers'] == []


def test_upgrade_target_is_not_a_retirement_date_or_deprecation_claim():
    value = _model({'id': 'future-model', 'upgradeTo': 'replacement',
                    'upgradeInfo': {'model': 'replacement', 'retirementAt': 2000},
                    'multiAgentVersion': 'v99', 'modelSpecialty': 'future-specialty'})
    assert value['retirement'] is None and value['deprecated'] is None
    assert value['upgrade'] == 'replacement' and value['upgrade_info']['retirementAt'] == 2000
    assert value['multi_agent_version'] == 'v99' and value['subagent_support'] is None
    assert value['model_specialty'] == 'future-specialty'


@pytest.mark.parametrize('items', [[{'id': 'ok'}, {'id': ['bad']}], [{'id': 'same'}, {'id': 'same'}], {}])
def test_catalog_drift_does_not_return_a_fresh_partial_catalog(tmp_path, monkeypatch, items):
    monkeypatch.setattr('agentbridge.error_observer.provider_version', lambda account: '1.0.31')
    monkeypatch.setattr(provider_catalog, 'models', lambda _: items)
    with Bridge(tmp_path) as bridge:
        bridge.register(Account('fixture', 'cursor'))
        result = bridge.models('cursor', account_ref='fixture', refresh=True)
        assert result['supported'] is False and result['stale'] is True
        assert result['models'] == [] and result['reason'] == 'provider_protocol_error'


def test_invalid_refresh_flags_are_not_coerced_to_network_requests(tmp_path):
    with Bridge(tmp_path) as bridge:
        with pytest.raises(BridgeError) as error:
            bridge.models('codex', refresh='false')
    assert error.value.code == 'invalid_input'


@pytest.mark.parametrize('result', [{}, {'models': []}, {'data': None}])
def test_codex_model_page_requires_published_data_field(tmp_path, monkeypatch, result):
    probe = CodexAppServerProbe(Account('fixture', 'codex', home=str(tmp_path)))
    monkeypatch.setattr(probe, '_rpc', lambda method, params: result)
    with pytest.raises(BridgeError) as error:
        probe.list_models()
    assert error.value.code == 'provider_protocol_error'


@pytest.mark.parametrize('account', [{}, {'type': 'future-auth-mode'}, ['chatgpt']])
def test_codex_account_shape_cannot_invent_authenticated_status(tmp_path, monkeypatch, account):
    probe = CodexAppServerProbe(Account('fixture', 'codex', home=str(tmp_path)))
    monkeypatch.setattr(probe, '_rpc', lambda method, params: {'account': account, 'requiresOpenaiAuth': 'false'})
    result = probe.read()
    assert result['status'] == 'provider_protocol_error'
    assert result['requires_openai_auth'] is None


def test_codex_unknown_auth_requirement_is_not_false(tmp_path, monkeypatch):
    probe = CodexAppServerProbe(Account('fixture', 'codex', home=str(tmp_path)))
    monkeypatch.setattr(probe, '_rpc', lambda method, params: {})
    result = probe.read()
    assert result['status'] == 'unknown' and result['requires_openai_auth'] is None
    assert stamp(0) == '1970-01-01T00:00:00+00:00'


def test_cursor_malformed_parameter_identifiers_fail_before_model_submission():
    with pytest.raises(BridgeError) as error:
        model_selection('fixture', 'high', sdk=None, api_key='fixture-key', catalog=[{
            'id': 'fixture', 'parameters': [{'id': ['reasoning_effort'], 'values': [{'value': 'high'}]}]}])
    assert error.value.code == 'provider_protocol_error'


def test_unknown_provider_is_not_routed_to_cursor_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_catalog, 'environment', lambda _: pytest.fail('must reject before credentials'))
    with pytest.raises(BridgeError) as error:
        provider_catalog.models(SimpleNamespace(engine='future-engine'))
    assert error.value.code == 'unsupported_operation'


def test_claude_truthy_auth_string_is_not_an_authenticated_profile(tmp_path, monkeypatch):
    account = Account('fixture', 'claude', home=str(tmp_path))
    monkeypatch.setattr(claude_account, 'environment', lambda _: {})
    monkeypatch.setattr(claude_account.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout=b'{"loggedIn":"false","email":"fixture@example.test"}'))
    with pytest.raises(BridgeError):
        claude_account.identity(account)
