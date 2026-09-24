"""Provider quota windows are explicit evidence, with no ambient credentials."""

from datetime import datetime, timedelta, timezone
import json
import secrets
import time

from agentbridge import Account, Bridge
from agentbridge.errors import BridgeError
from agentbridge.proxy import ManagementClient, ProxyRoute
from agentbridge.proxy.quota import active, passive_claude, passive_codex, project
from agentbridge.routing.service import RoutingService
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from test_accounts_service import configured_proxy, proxy_responses
from test_proxy_management import EMPTY_CONFIG, local_management


def _at(offset=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset)).isoformat()


def test_passive_codex_keeps_independent_reported_periods_and_named_pools():
    rows = passive_codex({'observed_at': _at(), 'signals': {
        'X-Codex-Primary-Used-Percent': '17',
        'X-Codex-Primary-Window-Minutes': '300',
        'X-Codex-Primary-Reset-After-Seconds': '180',
        'X-Codex-Secondary-Used-Percent': '56',
        'X-Codex-Secondary-Window-Minutes': '10080',
        'X-Codex-Additional-Next-Limit-Name': 'Future model pool',
        'X-Codex-Additional-Next-Primary-Used-Percent': '79',
        'Other-Private-Header': 'private-token',
    }})
    assert [(row['id'], row['used_percent'], row['window_seconds']) for row in rows] == [
        ('account:additional-next:primary', 79.0, None),
        ('account:base:primary', 17.0, 18000),
        ('account:base:secondary', 56.0, 604800),
    ]
    assert rows[0]['scope'] == 'unknown' and rows[0]['label'] == 'Future model pool · primary'
    assert rows[1]['remaining_percent'] == 83 and rows[1]['resets_at'] is not None
    assert 'private-token' not in repr(rows)


def test_passive_claude_fractional_5h_weekly_and_dynamic_scoped_windows():
    rows = passive_claude({'observed_at': _at(), 'signals': {
        'Anthropic-Ratelimit-Unified-5h-Utilization': '0.24',
        'Anthropic-Ratelimit-Unified-7d-Utilization': '0.63',
        'Anthropic-Ratelimit-Unified-7d-Reset': str(int(time.time()) + 20000),
        'Anthropic-Ratelimit-Unified-7d_oi-Utilization': '1.02',
        'Anthropic-Ratelimit-Unified-7d_oi-Status': 'rejected',
        'Anthropic-Ratelimit-Unified-9d_future-Utilization': '0.12',
        'Anthropic-Workspace-Id': 'private-workspace',
    }})
    by_id = {row['id']: row for row in rows}
    assert by_id['account:5h']['used_percent'] == 24
    assert by_id['account:5h']['window_seconds'] == 18000
    assert by_id['account:7d']['used_percent'] == 63
    assert by_id['account:7d']['remaining_percent'] == 37
    assert by_id['account:7d']['window_seconds'] == 604800
    assert by_id['account:7d_oi']['used_percent'] == 102
    assert by_id['account:7d_oi']['status'] == 'rejected'
    assert by_id['account:7d_oi']['scope'] == 'unknown'
    assert by_id['account:9d_future']['window_seconds'] == 777600
    assert 'private-workspace' not in repr(rows)


def test_active_codex_snake_case_windows_and_claude_structural_limits():
    observed = _at()
    codex = active('codex', {
        'rate_limit': {
            'primary_window': {'used_percent': 11, 'limit_window_seconds': 18000,
                               'reset_at': int(time.time()) + 100},
            'secondary_window': {'used_percent': 38, 'limit_window_seconds': 604800}},
        'additional_rate_limits': [{'limit_name': 'Future pool', 'rate_limit': {
            'primary_window': {'used_percent': 71, 'limit_window_seconds': 7200}}}],
        'private': 'private-body',
    }, observed)
    assert [(row['id'], row['used_percent']) for row in codex] == [
        ('base:primary', 11), ('base:secondary', 38), ('Future pool:primary', 71)]
    assert codex[0]['window_seconds'] == 18000 and codex[0]['resets_at'] is not None
    assert codex[2]['scope'] == 'unknown' and 'private-body' not in repr(codex)
    claude = active('claude', {'five_hour': {'utilization': 12, 'resets_at': _at(100)},
        'seven_day': {'utilization': 45, 'resets_at': _at(200)},
        'limits': [{'kind': 'weekly_scoped', 'percent': 74,
                    'scope': {'kind': 'model', 'model': {'id': 'future-model'}}},
                   {'kind': 'weekly_scoped', 'percent': 37,
                    'scope': {'kind': 'model_family',
                              'model_family': {'id': 'fable', 'display_name': 'Fable'}}}]}, observed)
    assert {row['used_percent'] for row in claude} == {12, 45, 74, 37}
    assert any(row['window_seconds'] == 604800 for row in claude)
    assert any(row['model_id'] == 'future-model' for row in claude)
    assert any(row.get('model_family') == 'fable' and row['label'] == 'Fable'
               and row['scope'] == 'model_family' for row in claude)


def test_projection_expires_on_ttl_or_reset_without_inventing_new_balance():
    observed = _at(-30)
    reset = _at(10)
    raw = passive_claude({'observed_at': observed, 'signals': {
        'Anthropic-Ratelimit-Unified-7d-Utilization': '0.31',
        'Anthropic-Ratelimit-Unified-7d-Reset': reset}})
    current = project(raw)
    assert current[0]['stale'] is False and current[0]['stale_at'] == raw[0]['resets_at']
    later = project(raw, now=time.time() + 20)
    assert later[0]['stale'] is True and later[0]['used_percent'] == 31


def test_mixed_window_ages_keep_fresh_usage_and_mark_old_window():
    current = RoutingService._usage_snapshot('fixture', {
        'source': 'cliproxy_management', 'quota_windows': [
            {'id': 'short', 'label': 'short', 'scope': 'account', 'model_id': None,
             'used_percent': 22, 'observed_at': _at(-3600), 'resets_at': None},
            {'id': 'long', 'label': 'long', 'scope': 'account', 'model_id': None,
             'used_percent': 61, 'observed_at': _at(), 'resets_at': None},
        ]})
    assert current['supported'] is True and current['stale'] is False
    assert current['reason'] is None
    assert [item['stale'] for item in current['quota_windows']] == [True, False]
    assert current['quota_windows'][0]['used_percent'] == 22


def test_active_fetch_rechecks_bound_identity_before_sending_token_placeholder(monkeypatch):
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', secrets.token_hex(16))
    monkeypatch.setattr(ManagementClient, '_post_json', lambda *args: 1 / 0)
    with local_management(proxy_responses()) as (port, _):
        route = ProxyRoute('codex-test', f'http://127.0.0.1:{port}/v1', 'FIXTURE_PROXY_KEY')
        client = ManagementClient(route, 'FIXTURE_MANAGEMENT_KEY')
        try:
            client.fetch_quota('0' * 64)
        except BridgeError as error:
            assert error.code == 'proxy_binding_unverified'
        else:
            raise AssertionError('An unrelated binding was accepted.')


def test_explicit_codex_refresh_uses_bound_proxy_and_persists_only_projection(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', secrets.token_hex(16))
    calls = []

    def post(self, path, secret, body):
        calls.append((path, body))
        assert path == '/v0/management/api-call'
        assert body['url'] == 'https://chatgpt.com/backend-api/wham/usage'
        assert body['auth_index'] == 'private-index'
        assert body['header']['Chatgpt-Account-Id'] == 'private-account'
        assert body['header']['Authorization'] == 'Bearer $TOKEN$'
        return {'status_code': 200, 'body': json.dumps({'rate_limit': {
            'primary_window': {'used_percent': 21, 'limit_window_seconds': 18000},
            'secondary_window': {'used_percent': 64, 'limit_window_seconds': 604800}},
            'private_error': 'raw provider secret'})}

    monkeypatch.setattr(ManagementClient, '_post_json', post)
    with local_management(proxy_responses(used_percent='5')) as (port, _):
        bridge = Bridge(tmp_path)
        configured_proxy(bridge, port)
        passive = bridge.account_usage('codex-test')
        current = bridge.account_usage('codex-test', refresh=True)
        cached = bridge.account_usage('codex-test')
    assert passive['source'] == 'cliproxy_management' and passive['quota_windows'][0]['used_percent'] == 5
    assert current['source'] == cached['source'] == 'cliproxy_upstream_usage'
    assert [item['used_percent'] for item in current['quota_windows']] == [21, 64]
    assert len(calls) == 1
    saved = json.dumps(bridge.store.usage_history('codex-test'))
    assert 'raw provider secret' not in saved and 'private-index' not in saved
    assert 'private-account' not in saved


def test_active_refresh_retains_distinct_passive_named_pool(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', secrets.token_hex(16))
    responses = proxy_responses(used_percent='5')
    signals = responses['/v0/management/auth-files'][1]['files'][0]['quota']['signals']
    signals['X-Codex-Additional-Future-Primary-Used-Percent'] = '29'

    def post(self, path, secret, body):
        return {'status_code': 200, 'body': json.dumps({'rate_limit': {
            'primary_window': {'used_percent': 21, 'limit_window_seconds': 18000}}})}

    monkeypatch.setattr(ManagementClient, '_post_json', post)
    with local_management(responses) as (port, _):
        bridge = Bridge(tmp_path)
        configured_proxy(bridge, port)
        current = bridge.account_usage('codex-test', refresh=True)
    assert {(row['label'], row['used_percent']) for row in current['quota_windows']} == {
        ('primary', 21), ('additional-future · primary', 29)}
    assert current['source'] == 'cliproxy_combined_usage'


def test_failed_active_refresh_keeps_newest_prior_observation_with_safe_reason(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', secrets.token_hex(16))
    older = proxy_responses(used_percent='6')
    old_time = _at(-7200)
    older['/v0/management/auth-files'][1]['files'][0]['quota']['observed_at'] = old_time
    attempts = 0

    def post(self, path, secret, body):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {'status_code': 200, 'body': json.dumps({'rate_limit': {
                'primary_window': {'used_percent': 73, 'limit_window_seconds': 18000}}})}
        raise BridgeError('proxy_observation_unavailable', 'private upstream error')

    monkeypatch.setattr(ManagementClient, '_post_json', post)
    with local_management(older) as (port, _):
        bridge = Bridge(tmp_path)
        configured_proxy(bridge, port)
        first = bridge.account_usage('codex-test', refresh=True)
        second = bridge.account_usage('codex-test', refresh=True)
    assert first['source'] == second['source'] == 'cliproxy_upstream_usage'
    assert second['quota_windows'][0]['used_percent'] == 73
    assert second['refresh_reason'] == 'proxy_observation_unavailable'
    assert 'private upstream error' not in repr(second)


def test_failed_refresh_does_not_revive_old_passive_percentage(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', secrets.token_hex(16))
    older = proxy_responses(used_percent='44')
    older['/v0/management/auth-files'][1]['files'][0]['quota']['observed_at'] = _at(-7200)
    monkeypatch.setattr(ManagementClient, '_post_json', lambda *args: {
        'status_code': 502, 'body': '{"error":"private upstream body"}'})
    with local_management(older) as (port, _):
        bridge = Bridge(tmp_path)
        configured_proxy(bridge, port)
        result = bridge.account_usage('codex-test', refresh=True)
    assert result['source'] == 'cliproxy_management'
    assert result['quota_windows'][0]['used_percent'] == 44
    assert result['quota_windows'][0]['stale'] is True
    assert result['stale'] is True and result['reason'] == 'upstream_quota_stale'
    assert result['refresh_reason'] == 'upstream_quota_unavailable'
    assert 'private upstream body' not in repr(result)


def test_explicit_claude_refresh_gets_weekly_and_preserves_unknown_scoped_claim(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', secrets.token_hex(16))
    responses = {
        '/v0/management/auth-files': (200, {'files': [{
            'name': 'one.json', 'source': 'file', 'runtime_only': False,
            'provider': 'claude', 'status': 'active', 'disabled': False,
            'unavailable': False, 'auth_index': 'claude-private-index',
            'account_type': 'oauth', 'email': 'user@example.test', 'cooldowns': [],
        }]}, {}),
        '/v0/management/config': (200, EMPTY_CONFIG, {}),
        '/v0/management/auth-files/models?name=one.json': (
            200, {'models': [{'id': 'claude-future'}]}, {}),
    }

    def post(self, path, secret, body):
        assert body['url'] == 'https://api.anthropic.com/api/oauth/usage'
        assert body['header']['anthropic-beta'] == 'oauth-2025-04-20'
        assert 'Chatgpt-Account-Id' not in body['header']
        return {'status_code': 200, 'body': json.dumps({
            'five_hour': {'utilization': 25, 'resets_at': _at(600)},
            'seven_day': {'utilization': 81, 'resets_at': _at(20000)},
            'seven_day_future': {'utilization': 40}})}

    monkeypatch.setattr(ManagementClient, '_post_json', post)
    with local_management(responses) as (port, _):
        bridge = Bridge(tmp_path)
        account = Account('claude-test', 'codex', name='Claude test', provider='claude',
            supported_models=('claude-future',), proxy_base_url=f'http://127.0.0.1:{port}/v1',
            key_env='FIXTURE_PROXY_KEY', management_key_env='FIXTURE_MANAGEMENT_KEY')
        seed_authenticated_proxy_account(bridge.store, account, observe_local=True,
                                         record_observation=False)
        result = bridge.account_usage('claude-test', refresh=True)
    assert result['source'] == 'cliproxy_upstream_usage'
    assert result['supported'] is True and result['stale'] is False
    rows = {item['id']: item for item in result['quota_windows']}
    assert rows['seven_day']['used_percent'] == 81
    assert rows['seven_day']['window_seconds'] == 604800
    assert rows['seven_day_future']['scope'] == 'unknown'
