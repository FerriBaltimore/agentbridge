"""Optional model controls must be bounded and safe to project."""

from agentbridge.proxy.model_catalog import catalog_metadata


def test_catalog_discards_untrusted_values_without_failing():
    catalog = catalog_metadata({'data': [
        {'slug': 'fixture/model', 'context_window': 120000,
         'supported_reasoning_levels': [{'effort': ['bad']}, {'effort': 'high'},
                                        {'effort': 'high'}, {'effort': 'private'}],
         'default_reasoning_level': 'high',
         'input_modalities': [{'bad': True}, 'text', 'text', 'private'],
         'secret': 'must-not-escape'},
        {'slug': ['bad'], 'context_window': 200000},
    ]})
    assert catalog == {'fixture/model': {
        'reasoning_efforts': ['high'], 'default_reasoning_effort': 'high',
        'context_windows': [120000], 'input_modalities': ['text'],
    }}


def test_codex_client_catalog_uses_models_and_reported_maximum():
    catalog = catalog_metadata({'models': [
        {'slug': 'fixture/codex', 'context_window': 131072,
         'max_context_window': 262144,
         'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'xhigh'}],
         'default_reasoning_level': 'xhigh',
         'input_modalities': ['text', 'image'],
         'private_token': 'must-not-escape'},
    ], 'data': [{'slug': 'stale/alternative', 'context_window': 900000}]})
    assert catalog == {'fixture/codex': {
        'reasoning_efforts': ['low', 'xhigh'],
        'default_reasoning_effort': 'xhigh',
        'context_windows': [262144], 'input_modalities': ['text', 'image'],
    }}


def test_client_catalog_keeps_unknown_controls_unknown():
    catalog = catalog_metadata({'models': [
        {'slug': 'fixture/unknown', 'context_window': True,
         'max_context_window': '262144',
         'supported_reasoning_levels': [{'effort': 'private'}]},
        {'slug': 'fixture/fallback', 'context_window': 65536,
         'max_context_window': 0},
    ]})
    assert catalog['fixture/unknown']['reasoning_efforts'] == []
    assert catalog['fixture/unknown']['context_windows'] == []
    assert catalog['fixture/fallback']['context_windows'] == [65536]
    assert catalog_metadata({'models': [{}] * 501,
                             'data': [{'slug': 'fixture/unsafe', 'context_window': 1}]}) == {}
