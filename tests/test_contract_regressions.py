import io
import json
import sys
import time

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.grantbridge import GrantBridgeClient
from agentbridge.rpc import rpc
from fixtures.test_proxy_account_fixture import (
    proxy_account, register_verified_proxy_account, seed_authenticated_proxy_account)


def stored_turn(bridge, tmp_path, label):
    register_verified_proxy_account(bridge.store, label, 18000 + sum(map(ord, label)))
    bridge.store.add_session('session-' + label, label, str(tmp_path), 'fixture-model')
    instance = bridge.get_session('session-' + label)
    run_id, _ = bridge.store.admit(label, instance['id'], 'hello', RunOptions(), None, message_id='message-' + label)
    return instance['id'], run_id


def test_message_pagination_counts_messages_not_runs(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    instance, run = stored_turn(bridge, tmp_path, 'first')
    bridge.store.emit(run, 'assistant', {'text': 'answer'})
    bridge.store.finish(run, 'completed')
    first = bridge.messages(instance, limit=1)
    second = bridge.messages(instance, limit=1, cursor=1)
    assert len(first) == len(second) == 1
    assert first[0]['role'] == 'user' and second[0]['role'] == 'assistant'
    assert not bridge.messages(instance, limit=1, cursor=2)
    assert bridge.messages(instance, role='assistant', limit=1) == second


def test_transcript_and_usage_include_observations_after_first_thousand_events(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    instance, run = stored_turn(bridge, tmp_path, 'long')
    for index in range(1005):
        bridge.store.emit(run, 'text_delta', {'text': 'x'})
    bridge.store.emit(run, 'assistant', {'text': 'complete answer'})
    bridge.store.emit(run, 'usage', {'scope': 'run', 'source': 'fixture', 'tokens': {'output_tokens': 1005}})
    bridge.store.finish(run, 'completed')
    assert bridge.run(run).text == 'complete answer'
    assert bridge.run(run).consumption['supported']
    assert bridge.messages(instance, role='assistant')[0]['content'] == 'complete answer'
    assert len(bridge.turn_events(run, limit=1)) == 1


def test_turn_filters_run_before_pagination(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    _, first = stored_turn(bridge, tmp_path, 'first')
    bridge.store.finish(first, 'completed')
    instance, second = stored_turn(bridge, tmp_path, 'second')
    assert bridge.turns(instance_id=instance, limit=1)[0]['turn_id'] == second


def test_unknown_outcomes_are_never_declared_retryable():
    assert not BridgeError('provider_timeout', 'test', outcome='unknown', retryable=True).retryable
    class TimeoutBridge:
        def capabilities(self, **_):
            raise TimeoutError()
    out = io.StringIO()
    rpc(TimeoutBridge(), io.StringIO('{"jsonrpc":"2.0","id":1,"method":"capabilities.get"}\n'), out)
    assert json.loads(out.getvalue())['error']['data']['retryable'] is False


def test_grantbridge_timeout_bounds_partial_unterminated_lines(tmp_path, monkeypatch):
    adapter = tmp_path / 'partial.py'
    adapter.write_text('import sys,time\nfor line in sys.stdin:\n sys.stdout.write("{");sys.stdout.flush();time.sleep(10)\n')
    client = GrantBridgeClient(adapter=adapter, node=sys.executable, timeout=1)
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', 'fixture-only-management-key')
    started = time.monotonic()
    try:
        with pytest.raises(BridgeError) as error:
            client.proxy_status('fixture-state', 'codex', 'http://127.0.0.1:8317/v1',
                                'FIXTURE_MANAGEMENT_KEY')
        assert error.value.code == 'grantbridge_timeout'
        assert time.monotonic() - started < 2
    finally:
        # Stop the deliberate broken fixture immediately; never leave jobs behind.
        client.process.kill()
        client.process.wait(timeout=2)
        client.close()


def test_proxy_account_does_not_fall_back_to_operator_credentials(tmp_path, monkeypatch):
    from agentbridge.credentials import environment
    monkeypatch.setenv('UNRELATED_API_KEY', 'operator-key-not-for-this-account')
    account = proxy_account('fixture', 18123)
    with pytest.raises(BridgeError) as error:
        environment(account)
    assert error.value.code == 'credential_unavailable'


def test_unsupported_stop_override_does_not_pretend_to_change_worker_grace(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    _, run = stored_turn(bridge, tmp_path, 'stop')
    with pytest.raises(BridgeError) as error:
        bridge.turn_stop(run, grace_period_ms=500)
    assert error.value.code == 'unsupported'
    assert not bridge.run(run).snapshot['stop_requested']


def test_public_transfer_rejects_unknown_parameters_before_creating_destination(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    instance, _ = stored_turn(bridge, tmp_path, 'source')
    seed_authenticated_proxy_account(bridge.store, proxy_account('target', 18234))
    out = io.StringIO()
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'instances.transfer',
               'params': {'instance_id': instance, 'target_account_ref': 'target', 'unexpected': True}}
    rpc(bridge, io.StringIO(json.dumps(request) + '\n'), out)
    assert json.loads(out.getvalue())['error']['data']['code'] == 'invalid_params'
    assert len(bridge.sessions()) == 1
