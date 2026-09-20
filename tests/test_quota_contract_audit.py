"""Sparse, malformed and future provider quota observations stay uncertain."""
import io
import json
from types import SimpleNamespace

import pytest

from agentbridge import Account
from agentbridge.claude_account import quota_request
from agentbridge.quota_scope import claude_limit
from agentbridge.quota_windows import project, reset_credits
from agentbridge.usage import snapshot
from agentbridge.usage_rollouts import last_token_count, has_windows, newest_rollouts, newest_token_count


def test_future_observation_is_stale_without_negative_or_invented_age():
    value = project('codex', {'quota': {'primary': {'usedPercent': 20}}, 'observed_at': 2000}, now=1000)
    assert value['stale'] is True and value['age_seconds'] is None
    assert value['observation_in_future'] is True and value['windows'][0]['used_percent'] == 20
    assert value['reset_credits']['stale'] is True


def test_codex_malformed_pool_identifier_does_not_crash_or_become_identity():
    value = project('codex', {'quota': {'rateLimits': {'limitId': ['bad'],
        'primary': {'usedPercent': 25}}}, 'observed_at': 1000}, now=1001)
    assert value['windows'][0]['pool_id'] == 'codex'
    assert value['windows'][0]['remaining_percent'] == 75


def test_native_no_expiry_is_distinct_from_missing_expiry():
    value = reset_credits({'availableCount': 3, 'credits': [
        {'id': 'no-expiry', 'expiresAt': None}, {'id': 'unknown'}, {'id': 'bad', 'expiresAt': 'nonsense'}]}, now=1000)
    none, missing, bad = value['credits']
    assert none['expiry_status'] == 'no_expiry' and none['expired'] is False
    assert missing['expiry_status'] == bad['expiry_status'] == 'unknown'
    assert missing['expired'] is bad['expired'] is None


def test_claude_sparse_structural_limit_keeps_reported_reset_without_utilization(monkeypatch):
    body = {'limits': [{'kind': 'new_pool', 'scope': {'kind': 'model', 'model': {'id': 'future'}},
                        'resets_at': 2000, 'window_seconds': 1000}]}
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: SimpleNamespace(
        open=lambda *args, **kwargs: io.BytesIO(json.dumps(body).encode())))
    value = quota_request('fixture-token')
    assert len(value['windows']) == 1
    assert value['windows'][0]['used_percent'] is None
    assert value['windows'][0]['model_id'] == 'future' and value['windows'][0]['resets_at']


@pytest.mark.parametrize('payload', [['token_count'], {'payload': ['token_count']},
                                      {'payload': {'type': 'token_count', 'rate_limits': ['bad']}}])
def test_malformed_rollout_shapes_do_not_crash_usage_queries(tmp_path, payload):
    path = tmp_path / 'rollout.jsonl'
    path.write_text(json.dumps(payload) + '\n')
    result = last_token_count(path)
    assert result is None or has_windows(result) is False


def test_future_or_naive_rollout_time_is_never_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr('agentbridge.usage.time.time', lambda: 1000)
    account = Account('fixture', 'codex', home=str(tmp_path))
    for at in ('1970-01-01T00:33:20+00:00', '1970-01-01T00:16:30'):
        monkeypatch.setattr('agentbridge.usage_rollouts.newest_token_count', lambda _: {
            'at': at, 'rate_limits': {'primary': {'used_percent': 30}}, 'info': ['bad']})
        result = snapshot(account)
        assert result['stale'] is True and result['tokens'] == {}


@pytest.mark.parametrize('status', [{}, [], {'future': 'rejected'}, ['rejected']])
def test_claude_unknown_status_shape_stays_unknown(status):
    value = project('claude', {'source': 'claude_stream', 'rateLimitType': 'five_hour',
                              'status': status, 'utilization': .25}, now=1000)
    assert value['windows'][0]['status'] is None
    assert value['windows'][0]['limit_reached'] is False


def test_codex_rollout_sessions_root_cannot_link_to_another_account(tmp_path):
    victim = tmp_path / 'other-account'
    folder = victim / 'sessions' / '2026' / '09' / '20'
    folder.mkdir(parents=True)
    path = folder / 'rollout-fixture.jsonl'
    path.write_text(json.dumps({'timestamp': '2026-09-20T12:00:00Z', 'payload': {
        'type': 'token_count', 'rate_limits': {'primary': {'used_percent': 37}}}}) + '\n')
    home = tmp_path / 'bound-account'
    home.mkdir()
    (home / 'sessions').symlink_to(victim / 'sessions', target_is_directory=True)
    assert newest_rollouts(victim) == [path]
    assert newest_rollouts(home) == []
    assert newest_token_count([home]) is None
    assert snapshot(Account('bound', 'codex', home=str(home)))['reason'] == 'no_local_observation'


def test_claude_explicit_periods_remain_distinct_from_legacy_alias():
    value = project('claude', {'limits': [
        {'kind': 'session', 'window_seconds': 3600, 'utilization': 10},
        {'kind': 'session', 'window_seconds': 7200, 'utilization': 20}],
        'five_hour': {'utilization': 30}}, now=1000)
    rows = value['windows']
    assert len(rows) == len({row['id'] for row in rows}) == 3
    assert [row['window_seconds'] for row in rows] == [3600, 7200, 18000]
    assert [row['used_percent'] for row in rows] == [10, 20, 30]
    assert rows[0]['id'].startswith('scoped:') and rows[1]['id'].startswith('scoped:')
    assert rows[2]['id'] == 'five_hour' and rows[2]['supplemental'] is True


@pytest.mark.parametrize('kind,alias,seconds', [('session', 'five_hour', 18000),
                                              ('weekly_all', 'seven_day', 604800)])
def test_claude_native_alias_requires_absent_or_matching_explicit_period(kind, alias, seconds):
    assert claude_limit({'kind': kind})['id'] == alias
    for duration in ({'window_seconds': seconds}, {'window_duration_seconds': seconds},
                     {'duration_seconds': seconds}, {'window': {'duration_seconds': seconds}}):
        assert claude_limit({'kind': kind, **duration})['id'] == alias
    for explicit in (3600, None, 'unknown', [], -1):
        assert claude_limit({'kind': kind, 'window_seconds': explicit})['id'].startswith('scoped:')
