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
