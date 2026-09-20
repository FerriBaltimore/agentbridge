"""Catalog fields from native schemas, with no real provider or credentials."""
import json

from agentbridge import Account, Bridge
from agentbridge.catalog import _model
from agentbridge.commands.model_actions import print_models


def test_codex_native_reasoning_options_are_available_in_public_catalog(tmp_path, monkeypatch):
    from agentbridge import catalog

    class Probe:
        def __init__(self, account):
            assert account.id == 'fixture'

        def list_models(self):
            return [
                {'id': 'fixture-model', 'model': 'wire-model', 'displayName': 'Fixture',
                 'hidden': False, 'isDefault': True, 'defaultReasoningEffort': 'high',
                 'supportedReasoningEfforts': [
                     {'reasoningEffort': 'high', 'description': 'More reasoning'},
                     {'reasoningEffort': 'future-level', 'description': 'Provider-defined'}],
                 'inputModalities': ['text', 'image'],
                 'serviceTiers': [{'id': 'fast', 'name': 'Fast', 'description': 'Faster'}],
                 'defaultServiceTier': 'fast'},
                {'id': 'hidden-model', 'hidden': True},
            ]

    monkeypatch.setattr(catalog, 'CodexAppServerProbe', Probe)
    with Bridge(tmp_path) as bridge:
        bridge.register(Account('fixture', 'codex', home=str(tmp_path)))
        result = bridge.models('codex', account_ref='fixture', refresh=True)
        assert len(result['items']) == 1
        model = result['items'][0]
        assert model['reasoning_efforts'] == ['high', 'future-level']
        assert model['reasoning_effort_options'][0]['description'] == 'More reasoning'
        assert model['default_reasoning_effort'] == 'high'
        assert model['default_service_tier'] == 'fast'
        assert model['resolved_model'] == 'wire-model'
        assert model['context_windows'] == []
        assert model['max_output_tokens'] is None
        assert model['stale'] is False and model['observed_at'] == result['observed_at']
        assert len(bridge.models('codex', account_ref='fixture', refresh=True, include_hidden=True)['items']) == 2
        assert bridge.runs() == []


def test_claude_initialize_effort_fields_do_not_invent_context_or_modalities():
    model = _model({'id': 'sonnet', 'resolvedModel': 'claude-fixture',
                    'displayName': 'Fixture (1M)', 'description': 'Extended context model',
                    'supportsEffort': True, 'supportedEffortLevels': ['low', 'high', 'max'],
                    'supportsAdaptiveThinking': True, 'supportsFastMode': False})
    assert model['reasoning_efforts'] == ['low', 'high', 'max']
    assert model['reasoning_support'] is True
    assert model['adaptive_thinking_support'] is True
    assert model['fast_mode_support'] is False
    assert model['auto_mode_support'] is None
    assert model['context_windows'] == []
    assert model['input_modalities'] == []
    assert model['max_input_tokens'] is None
    assert model['availability'] == 'unknown'


def test_cursor_parameters_and_variants_preserve_provider_choices_without_inference():
    model = _model({'id': 'fixture', 'display_name': 'Fixture', 'parameters': [
        {'id': 'reasoning_effort', 'display_name': 'Reasoning',
         'values': [{'value': 'high', 'display_name': 'High'}, {'value': 'ultra'}]},
        {'id': 'optimize_for', 'display_name': 'Optimize for', 'values': [{'value': 'cost'}]},
    ], 'variants': [{'display_name': 'High', 'is_default': True,
                     'params': [{'id': 'reasoning_effort', 'value': 'high'}]}]})
    assert model['reasoning_efforts'] == ['high', 'ultra']
    assert model['parameters'][1]['values'][0]['value'] == 'cost'
    assert model['variants'][0]['params'] == [{'id': 'reasoning_effort', 'value': 'high'}]
    assert model['variants'][0]['is_default'] is True
    assert model['default_reasoning_effort'] is None
    assert model['context_windows'] == []
    assert model['max_output_tokens'] is None


def test_cursor_observed_effort_parameter_is_not_confused_with_fast_mode():
    model = _model({'id': 'grok-4.6', 'display_name': 'Grok 4.6', 'parameters': [
        {'id': 'effort', 'display_name': 'Effort', 'values': [
            {'value': 'low'}, {'value': 'medium'}, {'value': 'high'}, {'value': 'xhigh'}]},
        {'id': 'fast', 'display_name': 'Fast', 'values': [{'value': 'false'}, {'value': 'true'}]},
    ]})
    assert model['reasoning_efforts'] == ['low', 'medium', 'high', 'xhigh']
    assert model['parameters'][1]['values'] == [
        {'value': 'false', 'display_name': 'false'}, {'value': 'true', 'display_name': 'true'}]


def test_invalid_and_absent_metadata_remains_unknown():
    model = _model({'id': 'fixture', 'supportedReasoningEfforts': [True, 0, {}, {'reasoningEffort': []}],
                    'context_windows': [True, 0, -1, '1000000', 200000, 200000],
                    'maxInputTokens': True, 'maxOutputTokens': 0,
                    'inputModalities': ['text', {}, False, 'text'], 'supportsEffort': 'false',
                    'parameters': [{'id': 'other', 'values': False}], 'variants': [{'params': False}]})
    assert model['context_windows'] == [200000]
    assert model['reasoning_efforts'] == []
    assert model['max_input_tokens'] is None and model['max_output_tokens'] is None
    assert model['input_modalities'] == ['text']
    assert model['reasoning_support'] is None
    assert model['parameters'][0]['values'] == []
    assert model['variants'][0]['params'] == []


def test_model_cli_exposes_known_metadata_unknowns_and_pagination(capsys):
    known = _model({'id': 'fixture', 'contextWindow': 200000, 'maxOutputTokens': 64000,
                    'supportedReasoningEfforts': [{'reasoningEffort': 'high', 'description': 'High'}],
                    'defaultReasoningEffort': 'high', 'inputModalities': ['text', 'image']})
    value = {'engine': 'codex', 'source': 'live', 'stale': False,
             'items': [known, _model({'id': 'unknown'})], 'has_more': True, 'next_cursor': 2}
    print_models(value)
    output = capsys.readouterr().out
    assert 'Reasoning effort: high (default: high)' in output
    assert 'Context window: 200,000 tokens' in output
    assert 'Maximum output: 64,000 tokens' in output
    assert 'Input modalities: text, image' in output
    assert 'Context window: unknown' in output and 'Reasoning effort: unknown' in output
    assert 'Next cursor: 2' in output
    print_models(value, as_json=True)
    assert json.loads(capsys.readouterr().out) == value
