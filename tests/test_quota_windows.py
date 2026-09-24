import json
import secrets
import time

import pytest

from agentbridge import Bridge
from agentbridge.quota_windows import normalize, project, reset_credits
from test_accounts_service import configured_proxy, proxy_responses
from test_proxy_management import local_management


def test_codex_multiple_buckets_do_not_duplicate_legacy_view_or_sum_limits():
    bucket = {'limitId': 'codex', 'primary': {'usedPercent': 0, 'windowDurationMins': 300, 'resetsAt': 2000},
              'secondary': {'usedPercent': 99, 'windowDurationMins': 10080, 'resetsAt': 9000}}
    raw = {'quota': {'rateLimits': bucket, 'rateLimitsByLimitId': {'codex': bucket,
                    'model_specific': {'primary': {'usedPercent': 125}}}}, 'observed_at': 1000}
    value = project('codex', raw, now=1001)
    assert len(value['windows']) == 3
    short, weekly, other = value['windows']
    assert short['remaining_percent'] == 100 and short['reset_after_seconds'] == 999
    assert weekly['window_seconds'] == 604800 and weekly['remaining_percent'] == 1
    assert other['remaining_percent'] == 0 and other['limit_reached'] is True
    assert other['resets_at'] is None and other['scope'] == 'account_pool'
    assert 'total' not in value


def test_claude_shared_model_and_unknown_pools_keep_their_identity():
    raw = {'five_hour': {'utilization': 12, 'resets_at': '2030-01-01T01:00:00Z'},
           'seven_day': {'utilization': 80}, 'seven_day_fable': {'utilization': 99},
           'future_pool': {'utilization': 30},
           'limits': [{'kind': 'weekly_all', 'percent': 80},
                      {'kind': 'weekly_model', 'percent': 50, 'scope': {'model': {'id': 'fable', 'display_name': 'Fable'}}},
                      {'kind': 'weekly_model', 'percent': 60, 'scope': {'model': {'id': 'sonnet', 'display_name': 'Sonnet'}}}]}
    rows = normalize('claude', raw, now=1000)
    assert len(rows) == 6 and len({r['id'] for r in rows}) == 6
    by_name = {row['name']: row for row in rows}
    assert by_name['five_hour']['scope'] == 'account' and by_name['five_hour']['window_seconds'] == 18000
    assert by_name['seven_day_fable']['model_family'] is None
    assert by_name['seven_day_fable']['scope'] == 'unknown'
    assert by_name['future_pool']['scope'] == 'unknown' and by_name['future_pool']['window_seconds'] is None
    assert rows[1]['model_id'] == 'fable' and rows[1]['remaining_percent'] == 50
    assert rows[2]['model_id'] == 'sonnet'


def test_replay_recomputes_countdown_without_inventing_a_reset():
    raw = {'windows': [{'name': 'five_hour', 'used_percent': 100, 'resets_at': 1050}],
           'observed_at': 1000, 'supported': True}
    first = project('claude', raw, now=1001)
    second = project('claude', first, now=1051)
    assert first['windows'][0]['reset_after_seconds'] == 49 and first['stale'] is False
    assert second['windows'][0]['id'] == first['windows'][0]['id']
    assert second['windows'][0]['reset_after_seconds'] == 0 and second['stale'] is True
    assert second['windows'][0]['remaining_percent'] == 0
    assert second['windows'][0]['reset_due'] is True


@pytest.mark.parametrize('invalid', [True, float('nan'), float('inf'), -1, '50'])
def test_invalid_numbers_are_unknown_never_zero(invalid):
    row = normalize('codex', {'primary': {'usedPercent': invalid, 'resetsAt': invalid}}, now=1000)[0]
    assert row['used_percent'] is None and row['remaining_percent'] is None


def test_credit_count_is_authoritative_and_missing_details_are_not_empty():
    assert reset_credits(None)['available_count'] is None
    assert reset_credits({'availableCount': 0}) == {'available_count': 0, 'status': 'none', 'credits': None}
    credits = reset_credits({'availableCount': 3, 'credits': [{'id': 'fixture-credit', 'status': 'available', 'expiresAt': 2000}]})
    assert credits['available_count'] == 3 and len(credits['credits']) == 1
    assert reset_credits({'availableCount': 0, 'credits': []})['credits'] == []


def test_failed_proxy_refresh_records_unknown_after_known_quota(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', secrets.token_hex(16))
    responses = proxy_responses(used_percent='20')
    with local_management(responses) as (port, _):
        bridge = Bridge(tmp_path)
        configured_proxy(bridge, port)
        first = bridge.account_usage('codex-test', refresh=True)
        responses['/v0/management/auth-files'] = (503, {'error': 'private'}, {})
        second = bridge.account_usage('codex-test', refresh=True)
    assert first['supported'] and not first['stale']
    assert second['stale'] and second['quota_windows'] == []
    assert second['reason'] == 'proxy_observation_unavailable'
    history = bridge.account_usage_history('codex-test')
    assert history[0]['stale'] and history[0]['data']['quota_windows'] == []
    assert history[1]['data']['quota_windows'][0]['used_percent'] == 20


def test_cached_proxy_usage_history_expires_without_network(tmp_path):
    bridge = Bridge(tmp_path)
    configured_proxy(bridge, 8317, local=False)
    bridge.store.usage_observation('codex-test', 'cliproxy_management', 'account',
        {'supported': True, 'quota_windows': [{'model_id': 'gpt-test',
         'used_percent': 20, 'observed_at': None}], 'reason': None},
        observed_at=time.time() - 120)
    assert bridge.account_usage_history('codex-test')[0]['data']['stale'] is True
    assert bridge.runs() == []


def test_proxy_model_catalog_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', secrets.token_hex(16))
    responses = proxy_responses()
    responses['/v0/management/auth-files/models?name=one.json'] = (
        200, {'models': [{'id': f'model-{i}'} for i in range(501)]}, {})
    with local_management(responses) as (port, _):
        bridge = Bridge(tmp_path)
        configured_proxy(bridge, port, local=False)
        catalog = bridge.models(account_ref='codex-test', refresh=True)
    assert catalog['models'][0]['availability'] == 'configured_unverified'
    assert bridge.store.latest_account_observation('codex-test')['data']['reason'] == 'proxy_observation_unavailable'


def test_usage_cli_shows_known_proxy_percent_and_unknown_reset(tmp_path, monkeypatch, capsys):
    from agentbridge.cli import main
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', secrets.token_hex(16))
    responses = proxy_responses(used_percent='100')
    with local_management(responses) as (port, _):
        configured_proxy(Bridge(tmp_path), port)
        main(['--root', str(tmp_path), 'accounts', 'usage', 'Codex test', '--refresh'])
        output = capsys.readouterr().out
        assert 'Window: primary (account)' in output
        assert 'Model: gpt-test' not in output
        assert 'Used: 100.0% | Remaining: 0%' in output
        assert 'Stale: no' in output
        assert 'Reset in:' not in output
        main(['--root', str(tmp_path), 'usage', '--account-ref', 'Codex test', '--json'])
        assert json.loads(capsys.readouterr().out)['quota_windows'][0]['used_percent'] == 100
