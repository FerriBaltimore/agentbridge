import json
import time
from datetime import datetime, timezone

import pytest

from agentbridge import Account, Bridge
from agentbridge.cli import main
from agentbridge.rpc import dispatch


def test_explicit_reset_rpc_cli_and_capabilities(tmp_path, monkeypatch, capsys):
    bridge = Bridge(tmp_path)
    account = bridge.register(Account('fixture', 'codex', home=str(tmp_path), name='Fixture Codex'))
    calls = []
    def consume(store, account, key, credit):
        calls.append((account.id, key, credit))
        return {'outcome': 'no_credit', 'request_key': key, 'replayed': False}
    monkeypatch.setattr('agentbridge.quota_resets.consume', consume)
    assert dispatch(bridge, 'accounts.quota.reset', {'account_ref': account.id,
        'idempotency_key': 'key', 'credit_id': 'credit'})['outcome'] == 'no_credit'
    main(['--root', str(tmp_path), 'accounts', 'quota-reset', 'Fixture Codex',
          '--idempotency-key', 'key-cli', '--json'])
    assert json.loads(capsys.readouterr().out)['request_key'] == 'key-cli'
    assert calls == [('fixture', 'key', 'credit'), ('fixture', 'key-cli', None)]
    assert bridge.capabilities('claude')['operations']['accounts.quota.reset']['support'] == 'unsupported'


def test_usage_include_quota_keeps_live_observation(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path)
    bridge.register(Account('fixture', 'codex', home=str(tmp_path)))
    bridge.store.usage_observation('fixture', 'codex_app_server', 'account', {
        'quota': {'rateLimits': {'primary': {'usedPercent': 50}}}})
    monkeypatch.setattr(bridge, 'quota', lambda *args: pytest.fail('must not overwrite observed quota with legacy view'))
    value = bridge.usage('account', account_ref='fixture', include_quota=True)
    assert value['quota']['rateLimits']['primary']['usedPercent'] == 50
    assert value['windows'][0]['remaining_percent'] == 50


def test_rollout_history_keeps_original_observation_age(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path)
    bridge.register(Account('fixture', 'codex', home=str(tmp_path)))
    original = time.time() - 3600
    monkeypatch.setattr('agentbridge.usage_rollouts.newest_token_count', lambda _: {
        'at': datetime.fromtimestamp(original, timezone.utc).isoformat(),
        'rate_limits': {'primary': {'used_percent': 20, 'window_minutes': 300}}})
    value = bridge.account_usage('fixture')
    history = bridge.account_usage_history('fixture')
    assert abs(history[0]['observed_at'] - original) < .01
    assert value['stale'] and history[0]['data']['stale']


def test_cli_error_keeps_safe_structured_metadata(tmp_path, capsys):
    bridge = Bridge(tmp_path)
    bridge.register(Account('fixture', 'claude', home=str(tmp_path), name='Claude fixture'))
    with pytest.raises(SystemExit):
        main(['--root', str(tmp_path), 'accounts', 'quota-reset', 'Claude fixture', '--idempotency-key', 'key'])
    value = json.loads(capsys.readouterr().err)
    assert value['data']['code'] == 'unsupported_operation'
    assert value['data']['outcome'] == 'not_started' and value['data']['retryable'] is False
