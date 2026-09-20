import json
import time

import pytest

from agentbridge import Account, Bridge
from agentbridge.account_probe import CodexAppServerProbe
from agentbridge.errors import BridgeError
from agentbridge.quota_windows import normalize, project, reset_credits


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
    assert rows[0]['scope'] == 'account' and rows[0]['window_seconds'] == 18000
    assert rows[2]['model_family'] == 'fable'
    assert rows[3]['scope'] == 'unknown' and rows[3]['window_seconds'] is None
    assert rows[4]['model_id'] == 'fable' and rows[4]['remaining_percent'] == 50
    assert rows[5]['model_id'] == 'sonnet'


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


def test_failed_codex_refresh_invalidates_persisted_cache(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path)
    bridge.register(Account('fixture', 'codex', home=str(tmp_path)))
    def success(*args, **kwargs):
        return {'status': 'authenticated', 'quota': {'rateLimits': {'primary': {'usedPercent': 20}}}}
    monkeypatch.setattr(CodexAppServerProbe, 'read', success)
    first = bridge.account_usage('fixture', refresh=True)
    def failed(*args, **kwargs):
        raise BridgeError('authentication_required', 'Fixture missing authentication.')
    monkeypatch.setattr(CodexAppServerProbe, 'read', failed)
    second = bridge.account_usage('fixture', refresh=True)
    assert second['stale'] and second['windows'] == first['windows']
    assert second['reason'] == 'authentication_required'
    assert Bridge(tmp_path).account_usage('fixture')['stale']


def test_cached_observation_expires_without_network(tmp_path):
    bridge = Bridge(tmp_path)
    bridge.register(Account('fixture', 'codex', home=str(tmp_path)))
    bridge.store.usage_observation('fixture', 'codex_app_server', 'account',
        {'quota': {'rateLimits': {'primary': {'usedPercent': 20}}}}, observed_at=time.time() - 120)
    assert bridge.account_usage('fixture')['stale'] is True
    assert bridge.runs() == []


def test_model_pagination_and_cycle_are_bounded(tmp_path, monkeypatch):
    probe = CodexAppServerProbe(Account('fixture', 'codex', home=str(tmp_path)))
    calls = []
    def rpc(method, params):
        if method != 'model/list':
            return {}
        calls.append(params)
        return {'data': [{'id': str(len(calls))}], 'nextCursor': 'page2' if len(calls) == 1 else None}
    monkeypatch.setattr(probe, '_rpc', rpc)
    assert len(probe.list_models()) == 2
    assert calls[1]['cursor'] == 'page2' and calls[0]['includeHidden'] is True
    monkeypatch.setattr(probe, '_rpc', lambda *args: {'data': [], 'nextCursor': 'loop'})
    with pytest.raises(BridgeError, match='cursor'):
        probe.list_models()


def test_usage_cli_shows_windows_and_reset_zero(tmp_path, capsys):
    from agentbridge.cli import main
    bridge = Bridge(tmp_path)
    bridge.register(Account('fixture', 'claude', home=str(tmp_path), name='Quota fixture'))
    bridge.store.usage_observation('fixture', 'claude_oauth_usage', 'account', {
        'supported': True, 'windows': [{'name': 'seven_day_fable', 'used_percent': 100, 'resets_at': time.time() - 10}]})
    main(['--root', str(tmp_path), 'accounts', 'usage', 'Quota fixture'])
    output = capsys.readouterr().out
    assert 'Remaining: 0%' in output and 'Reset in: 0s' in output
    assert 'Model: fable' in output and 'Stale: yes' in output
    main(['--root', str(tmp_path), 'usage', '--account-ref', 'Quota fixture', '--json'])
    assert json.loads(capsys.readouterr().out)['windows'][0]['remaining_percent'] == 0
