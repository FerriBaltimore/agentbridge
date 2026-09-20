"""Reasoning selection uses provider choices and never submits a model turn."""
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agentbridge import BridgeError
from agentbridge.cursor_options import model_selection


@dataclass
class ParameterValue:
    id: str
    value: str


@dataclass
class Selection:
    id: str
    params: list


def fixture_sdk(catalog):
    calls = []

    def read(*, api_key):
        calls.append(api_key)
        return catalog

    sdk = SimpleNamespace(Cursor=SimpleNamespace(models=SimpleNamespace(list=read)),
                          ModelSelection=Selection, ModelParameterValue=ParameterValue)
    return sdk, calls


def test_cursor_effort_uses_exact_model_parameter_and_explicit_credential():
    catalog = [SimpleNamespace(id='fixture', parameters=(
        SimpleNamespace(id='reasoning_effort', values=(SimpleNamespace(value='high'), SimpleNamespace(value='future-effort'))),
        SimpleNamespace(id='optimize_for', values=(SimpleNamespace(value='cost'),)),
    ))]
    sdk, calls = fixture_sdk(catalog)
    selection = model_selection('fixture', 'future-effort', sdk=sdk, api_key='fixture-key')
    assert calls == ['fixture-key']
    assert selection.id == 'fixture'
    assert selection.params == [ParameterValue('reasoning_effort', 'future-effort')]


def test_cursor_without_effort_preserves_model_without_catalog_query():
    sdk, calls = fixture_sdk([])
    assert model_selection('fixture', None, sdk=sdk, api_key='fixture-key') == 'fixture'
    assert calls == []


@pytest.mark.parametrize(('model', 'effort', 'catalog', 'code'), [
    (None, 'high', [], 'unsupported_parameter'),
    ('fixture', 'high', [], 'model_unavailable'),
    ('fixture', 'high', [{'id': 'other'}], 'model_unavailable'),
    ('fixture', 'high', [{'id': 'fixture'}], 'unsupported_parameter'),
    ('fixture', 'high', [{'id': 'fixture', 'parameters': [
        {'id': 'optimize_for', 'values': [{'value': 'high'}]}]}], 'unsupported_parameter'),
    ('fixture', 'high', [{'id': 'fixture', 'parameters': [
        {'id': 'reasoning_effort', 'values': [{'value': 'low'}]}]}], 'unsupported_parameter'),
    ('fixture', '', [], 'unsupported_parameter'),
])
def test_cursor_never_silently_drops_or_substitutes_requested_effort(model, effort, catalog, code):
    sdk, _ = fixture_sdk(catalog)
    with pytest.raises(BridgeError) as error:
        model_selection(model, effort, sdk=sdk, api_key='fixture-key')
    assert error.value.code == code
    assert error.value.outcome == 'not_started'


def test_catalog_failure_is_safe_and_does_not_fall_back_to_unconfigured_model():
    def failure(**_):
        raise RuntimeError('fixture-key in a provider error')

    sdk, _ = fixture_sdk([])
    sdk.Cursor.models.list = failure
    with pytest.raises(BridgeError) as error:
        model_selection('fixture', 'high', sdk=sdk, api_key='fixture-key')
    assert error.value.code == 'provider_unavailable'
    assert 'fixture-key' not in str(error.value)
    assert error.value.outcome == 'not_started'


def test_preobserved_cursor_catalog_preserves_native_parameter_id():
    sdk, calls = fixture_sdk([])
    selection = model_selection('fixture', 'high', sdk=sdk, api_key='fixture-key', catalog=[
        {'id': 'fixture', 'parameters': [{'id': 'reasoningEffort', 'values': [{'value': 'high'}]}]}])
    assert selection.params == [ParameterValue('reasoningEffort', 'high')]
    assert calls == []


def test_cursor_observed_effort_id_passes_through_and_does_not_set_fast_mode():
    sdk, calls = fixture_sdk([{'id': 'grok-4.6', 'parameters': [
        {'id': 'effort', 'display_name': 'Effort', 'values': [
            {'value': 'low'}, {'value': 'medium'}, {'value': 'high'}, {'value': 'xhigh'}]},
        {'id': 'fast', 'display_name': 'Fast', 'values': [{'value': 'false'}, {'value': 'true'}]},
    ]}])
    selection = model_selection('grok-4.6', 'xhigh', sdk=sdk, api_key='fixture-key')
    assert calls == ['fixture-key']
    assert selection.params == [ParameterValue('effort', 'xhigh')]
    with pytest.raises(BridgeError) as error:
        model_selection('grok-4.6', 'true', sdk=sdk, api_key='fixture-key')
    assert error.value.code == 'unsupported_parameter'
