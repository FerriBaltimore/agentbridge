"""Quota normalization follows provider metadata instead of model-name tables."""
from agentbridge.quota_scope import claude_limit, legacy_window
from agentbridge.quota_windows import normalize, project, reset_credits


def test_previously_unseen_model_and_surface_preserve_reported_scope():
    raw = {'kind': 'rolling_future', 'group': 'business', 'window_seconds': 23456,
           'scope': {'kind': 'model_surface', 'model': {'id': 'future-model-2050', 'display_name': 'Future'},
                     'surface': {'id': 'editor-2050', 'display_name': 'Future editor'}}, 'percent': 11}
    row = normalize('claude', {'limits': [raw]}, now=1000)[0]
    assert row['model_id'] == 'future-model-2050' and row['model_family'] is None
    assert row['scope'] == row['scope_kind'] == 'model_surface'
    assert row['window_seconds'] == 23456 and row['group'] == 'business'
    assert row['scope_source'] == row['window_duration_source'] == 'provider'
    assert row['remaining_percent'] == 89


def test_reported_family_is_supported_without_a_release_or_model_guess():
    row = claude_limit({'kind': 'weekly_scoped', 'scope': {
        'kind': 'model_family', 'model_family': {'id': 'new-family', 'display_name': 'New family'}}})
    assert row['model_family'] == 'new-family' and row['model_family_label'] == 'New family'
    assert row['model_id'] is None and row['scope'] == 'model_family'


def test_legacy_names_describe_periods_but_never_identify_model_families():
    for suffix in ['fable', 'opus', 'sonnet', 'brand_new_family', 'overage_included']:
        value = legacy_window('seven_day_' + suffix)
        assert value['window_seconds'] == 604800
        assert value['scope'] == 'unknown' and value['model_family'] is None
    assert legacy_window('12_hour_future')['window_seconds'] == 43200
    assert legacy_window('two_week_future')['window_seconds'] == 1209600
    assert legacy_window('weeklyword')['window_seconds'] is None
    assert legacy_window('0_hour_future')['window_seconds'] is None
    assert legacy_window('one_month_future')['window_seconds'] is None


def test_sparse_structural_response_keeps_legacy_evidence_as_supplemental():
    rows = normalize('claude', {'limits': [{'kind': 'weekly_all', 'percent': 40}],
        'seven_day': {'utilization': 99}, 'five_hour': {'utilization': 10},
        'seven_day_future': {'utilization': 25}}, now=1000)
    assert [row['name'] for row in rows] == ['seven_day', 'five_hour', 'seven_day_future']
    assert rows[0]['used_percent'] == 40 and rows[0]['scope_source'] == 'provider'
    assert rows[1]['supplemental'] is True and rows[2]['supplemental'] is True
    assert all('total' not in row for row in rows)


def test_invalid_structural_limits_fall_back_to_legacy_without_claiming_overlap():
    rows = normalize('claude', {'limits': [{'kind': False}], 'seven_day_future': {'percent': 20}}, now=1000)
    assert len(rows) == 1 and rows[0]['supplemental'] is False
    assert rows[0]['used_percent'] == 20


def test_explicit_period_wins_and_scope_kind_participates_in_identity():
    item = {'kind': 'weekly_scoped', 'window': {'duration_seconds': 777},
            'scope': {'kind': 'new_scope', 'model': {'id': 'm', 'display_name': 'Old label'}}}
    first = claude_limit(item)
    changed_label = {**item, 'scope': {**item['scope'], 'model': {'id': 'm', 'display_name': 'New label'}}}
    changed_kind = {**item, 'scope': {**item['scope'], 'kind': 'different_scope'}}
    assert first['window_seconds'] == 777
    assert first['id'] == claude_limit(changed_label)['id']
    assert first['id'] != claude_limit(changed_kind)['id']
    assert claude_limit({**item, 'window_seconds': True})['window_seconds'] is None


def test_provider_identity_and_sparse_scope_metadata_are_preserved():
    first = claude_limit({'id': 'reported-pool', 'scope': {'kind': 'all'}, 'duration_seconds': 900})
    assert first['id'] == 'reported-pool' and first['scope'] == 'account'
    assert first['window_seconds'] == 900 and first['kind'] is None
    unknown = claude_limit({'id': 'future-pool', 'scope': {'kind': 'unseen-scope'}})
    assert unknown['scope_kind'] == 'unseen-scope' and unknown['scope'] == 'unknown'


def test_cached_guessed_family_is_removed_but_structural_evidence_survives():
    rows = normalize('claude', {'windows': [
        {'name': 'seven_day_future', 'used_percent': 99, 'scope': 'model_family', 'model_family': 'guessed'},
        {'name': 'declared', 'scope': 'model_family', 'model_family': 'reported', 'scope_source': 'provider'}]}, now=1000)
    assert rows[0]['scope'] == 'unknown' and rows[0]['model_family'] is None
    assert rows[1]['scope'] == 'model_family' and rows[1]['model_family'] == 'reported'


def test_stream_is_fraction_and_oauth_is_percent_with_dynamic_pool_names():
    stream = normalize('claude', {'source': 'claude_stream', 'rateLimitType': 'seven_day_new',
                                 'utilization': .5}, now=1000)[0]
    oauth = normalize('claude', {'seven_day_new': {'utilization': .5}}, now=1000)[0]
    assert stream['used_percent'] == 50 and oauth['used_percent'] == .5
    assert stream['window_seconds'] == oauth['window_seconds'] == 604800
    assert stream['model_family'] is None and oauth['model_family'] is None


def test_reset_expiry_countdown_is_recomputed_without_rewriting_provider_count():
    raw = {'quota': {'rateLimitResetCredits': {'availableCount': 7, 'credits': [
        {'id': 'fixture-credit', 'status': 'available', 'expiresAt': 1020}]}},
        'observed_at': 1000, 'source': 'codex_app_server'}
    first = project('codex', raw, now=1001)['reset_credits']
    second = project('codex', raw, now=1021)['reset_credits']
    assert first['credits'][0]['expires_in_seconds'] == 19 and first['credits'][0]['expired'] is False
    assert first['stale'] is False and first['age_seconds'] == 1
    assert second['credits'][0]['expires_in_seconds'] == 0 and second['credits'][0]['expired'] is True
    assert second['stale'] is True
    assert first['available_count'] == second['available_count'] == 7
    assert first['status'] == second['status'] == 'available'


def test_reset_credit_expiry_unknown_and_empty_details_remain_distinct():
    assert reset_credits({'availableCount': 2}, now=1000)['credits'] is None
    assert reset_credits({'availableCount': 2, 'credits': []}, now=1000)['credits'] == []
    row = reset_credits({'availableCount': 2, 'credits': [{'id': 'no-expiration'}]}, now=1000)['credits'][0]
    assert row['expires_at'] is row['expires_in_seconds'] is row['expired'] is None
    assert project('codex', {'quota': {'rateLimitResetCredits': {'availableCount': 2}}}, now=1000)['reset_credits']['stale']


def test_stream_explicit_duration_wins_over_temporal_compatibility():
    row = normalize('claude', {'source': 'claude_stream', 'rateLimitType': 'seven_day_new',
                               'utilization': .25, 'window_seconds': 900}, now=1000)[0]
    assert row['window_seconds'] == 900


def test_billing_utilization_is_not_a_time_window():
    rows = normalize('claude', {'extra_usage': {'utilization': 25, 'monthly_limit': 100},
                               'seven_day': {'utilization': 10}}, now=1000)
    assert [row['name'] for row in rows] == ['seven_day']


def test_overflowing_numbers_do_not_crash_account_observations():
    giant = 10 ** 400
    row = normalize('claude', {'limits': [{'kind': 'session', 'window_seconds': giant,
                                          'percent': giant}]}, now=1000)[0]
    assert row['window_seconds'] is None and row['used_percent'] is None


def test_two_explicit_periods_for_the_same_model_are_distinct_windows():
    short = {'kind': 'rolling', 'window_seconds': 3600, 'scope': {'model': {'id': 'future-model'}}}
    long = {**short, 'window_seconds': 7200}
    rows = normalize('claude', {'limits': [short, long]}, now=1000)
    assert len(rows) == 2 and rows[0]['id'] != rows[1]['id']


def test_credit_details_survive_sparse_count_without_inventing_total():
    value = reset_credits({'credits': [{'id': 'observed-credit', 'expiresAt': 1100}]}, now=1000)
    assert value['available_count'] is None and value['status'] == 'unknown'
    assert value['credits'][0]['expires_in_seconds'] == 100
