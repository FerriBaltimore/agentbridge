"""Claude quota scope identities do not collapse model or product buckets."""
from agentbridge.quota_scope import claude_limit


def test_different_surface_pools_have_distinct_ids_and_labels():
    first = claude_limit({'kind': 'weekly_scoped', 'group': 'weekly', 'scope': {'surface': {'display_name': 'Code'}}})
    second = claude_limit({'kind': 'weekly_scoped', 'group': 'weekly', 'scope': {'surface': {'display_name': 'Chat'}}})
    assert first['id'] != second['id']
    assert first['label'] == 'Code' and first['scope'] == 'surface'
    assert first['surface_label'] == 'Code' and first['surface_id'] is None
    assert first['window_seconds'] == second['window_seconds'] == 604800


def test_model_label_is_not_a_native_model_id():
    result = claude_limit({'kind': 'weekly_scoped', 'scope': {'model': {'display_name': 'Fable'}}})
    assert result['model_id'] is None
    assert result['model_label'] == result['label'] == 'Fable'
    assert result['scope'] == 'model'


def test_combined_scope_includes_both_dimensions():
    result = claude_limit({'kind': 'weekly_scoped', 'scope': {
        'model': {'id': 'provider-model', 'display_name': 'Model'},
        'surface': {'id': 'product', 'display_name': 'Code'}}})
    assert result['model_id'] == 'provider-model' and result['surface_id'] == 'product'
    assert result['label'] == 'Model / Code' and result['scope'] == 'model_surface'


def test_identity_depends_on_scope_and_group_but_not_usage():
    item = {'kind': 'weekly_scoped', 'group': 'weekly', 'percent': 20,
            'scope': {'model': {'display_name': 'Fable'}}}
    first = claude_limit(item)
    assert first['id'] == claude_limit({**item, 'percent': 90, 'resets_at': '2030-01-01T00:00:00Z'})['id']
    assert first['id'] != claude_limit({**item, 'group': 'other'})['id']


def test_account_aliases_are_used_only_for_unscoped_windows():
    assert claude_limit({'kind': 'session'})['name'] == 'five_hour'
    assert claude_limit({'kind': 'weekly_all'})['name'] == 'seven_day'
    scoped = claude_limit({'kind': 'weekly_all', 'scope': {'surface': {'display_name': 'Code'}}})
    assert scoped['name'] != 'seven_day' and scoped['scope'] == 'surface'


def test_invalid_scopes_are_unknown_and_names_remain_bounded():
    assert claude_limit({'kind': False}) is None
    result = claude_limit({'kind': 'new-kind', 'scope': {'model': ['malformed']}})
    assert result['scope'] == 'unknown' and result['window_seconds'] is None
    bounded = claude_limit({'kind': 'k' * 512, 'scope': {'model': {'display_name': 'm' * 512}}})
    assert len(bounded['id']) < 128
