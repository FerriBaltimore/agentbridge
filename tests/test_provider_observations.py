import io
import json
from types import SimpleNamespace
import urllib.error

import pytest

from agentbridge import Account, Bridge
from agentbridge.claude_account import quota
from agentbridge.errors import BridgeError
from agentbridge.permissions import Permissions
from agentbridge.models import RunOptions


def test_claude_quota_uses_only_bound_credential_and_sanitizes_output(tmp_path, monkeypatch):
    (tmp_path / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {'accessToken': 'fixture-secret'}}))
    account = Account('claude-test', 'claude', home=str(tmp_path))
    def request(req, timeout):
        assert req.full_url == 'https://api.anthropic.com/api/oauth/usage'
        assert req.headers['Authorization'] == 'Bearer fixture-secret'
        assert timeout == 15
        return io.BytesIO(json.dumps({'five_hour': {'utilization': 23, 'resets_at': '2026-09-21T00:00:00Z'},
                                     'bad': {'utilization': True}, 'extra': {'token': 'fixture-secret'}}).encode())
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: SimpleNamespace(open=request))
    value = quota(account)
    assert len(value['windows']) == 1
    window = value['windows'][0]
    assert window['name'] == 'five_hour' and window['used_percent'] == 23
    assert window['remaining_percent'] == 77 and window['scope'] == 'account'
    assert window['resets_at'] == '2026-09-21T00:00:00+00:00'
    assert 'fixture-secret' not in json.dumps(value)
    assert value['provider_contract'] == 'native_oauth_compatibility'


@pytest.mark.parametrize('status,code', [(401, 'authentication_required'), (429, 'rate_limited'), (500, 'provider_unavailable')])
def test_claude_usage_failure_never_returns_a_zero_quota(tmp_path, monkeypatch, status, code):
    (tmp_path / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {'accessToken': 'fixture-secret'}}))
    def request(*args, **kwargs):
        raise urllib.error.HTTPError('https://api.anthropic.com', status, 'PRIVATE BODY', {}, None)
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: SimpleNamespace(open=request))
    with pytest.raises(BridgeError) as error:
        quota(Account('claude-test', 'claude', home=str(tmp_path)))
    assert error.value.code == code
    assert 'PRIVATE BODY' not in str(error.value)


def test_claude_failed_refresh_preserves_old_observation_as_stale(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / 'state')
    bridge.register(Account('claude-test', 'claude', home=str(tmp_path)))
    monkeypatch.setattr('agentbridge.claude_account.identity', lambda _: {'status': 'loaded_only'})
    monkeypatch.setattr('agentbridge.claude_account.quota', lambda _: {
        'source': 'claude_oauth_usage', 'scope': 'account', 'supported': True, 'stale': False,
        'windows': [{'name': 'five_hour', 'used_percent': 23}]})
    first = bridge.account_usage('claude-test', refresh=True)
    monkeypatch.setattr('agentbridge.claude_account.identity', lambda _: {'status': 'authentication_required'})
    value = bridge.account_usage('claude-test', refresh=True)
    assert value['stale'] and value['observed_at'] == first['observed_at']
    assert value['windows'] == first['windows']
    assert bridge.account_usage('claude-test')['stale']
    assert bridge.runs() == []


def test_cursor_images_use_sdk_message_shape(tmp_path, monkeypatch):
    import runpy
    from pathlib import Path
    from agentbridge.cursor_worker import execute
    fixture = runpy.run_path(str(Path(__file__).parent / 'fixtures' / 'test_cursor_sdk_provider.py'))
    sdk = fixture['sdk']
    captured = []
    monkeypatch.setenv('FIXTURE_CURSOR_KEY', 'fixture-key-never-real')
    monkeypatch.setattr(sdk.Agent, 'send', lambda self, message: (captured.append(message) or self))
    execute({'cwd': str(tmp_path), 'model': 'fixture', 'key_env': 'FIXTURE_CURSOR_KEY',
             'prompt': 'hello', 'attachments': [{'type': 'image', 'media_type': 'image/png', 'data': 'fixture'}]},
            lambda _: None, sdk=sdk)
    assert captured == [{'text': 'hello', 'images': [{'mime_type': 'image/png', 'data': 'fixture'}]}]


def test_expired_permission_is_denied_and_cannot_be_revived(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    bridge.register(Account('fixture', 'codex', home=str(tmp_path)))
    instance = bridge.instance_create(account_ref='fixture', workspace_path=str(tmp_path))
    turn, _ = bridge.store.admit('test-turn', instance['id'], 'fixture', RunOptions(), 'key')
    permissions = Permissions(bridge.store)
    request = permissions.request(turn, {'operation': 'fixture'}, timeout=0)
    assert permissions.wait(turn, request) == 'deny'
    with pytest.raises(BridgeError) as error:
        permissions.respond(turn, request, 'allow')
    assert error.value.code == 'permission_expired'
    bridge.store.finish(turn, 'cancelled')


def test_cursor_account_quota_does_not_claim_remaining_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr('agentbridge.error_observer.provider_version', lambda account: '1.0.31')
    bridge = Bridge(tmp_path)
    bridge.register(Account('cursor-test', 'cursor'))
    value = bridge.account_usage('cursor-test', refresh=True)
    assert value['supported'] is False and value['stale'] is True
    assert value['reason'] == 'sdk_account_quota_unavailable'
    assert 'remaining' not in value and 'windows' not in value


@pytest.mark.parametrize('failure', [False, True])
def test_cursor_catalog_is_explicit_and_falls_back_without_inference(tmp_path, monkeypatch, failure):
    monkeypatch.setattr('agentbridge.error_observer.provider_version', lambda account: '1.0.31')
    from agentbridge import provider_catalog
    bridge = Bridge(tmp_path)
    monkeypatch.setenv('FIXTURE_CURSOR_KEY', 'fixture-only-key')
    bridge.register(Account('cursor-test', 'cursor', key_env='FIXTURE_CURSOR_KEY'))
    def launch(command, **kwargs):
        assert command[-1] == 'FIXTURE_CURSOR_KEY'
        assert kwargs['env']['FIXTURE_CURSOR_KEY'] == 'fixture-only-key'
        if failure:
            raise BridgeError('provider_unavailable', 'The provider model catalog is unavailable.')
        return json.dumps([{'id': 'model-fixture', 'display_name': 'Fixture', 'description': 'fixture'}]).encode()
    monkeypatch.setattr(provider_catalog, 'read_output', launch)
    result = bridge.models('cursor', account_ref='cursor-test', refresh=True)
    assert result['source'] == ('static' if failure else 'live')
    assert result['stale'] is failure
    if failure:
        assert result['reason'] == 'provider_unavailable'
    else:
        assert result['models'][0]['id'] == 'model-fixture'
        assert result['models'][0]['availability'] == 'unknown'
    assert not bridge.runs()
    assert 'fixture-only-key' not in json.dumps(result)


def test_native_channel_timeout_is_bounded_for_an_incomplete_line(tmp_path):
    import os
    import sys
    from agentbridge.provider_channel import ProviderChannel
    with ProviderChannel([sys.executable, '-c', 'import sys,time;sys.stdout.write("{");sys.stdout.flush();time.sleep(20)'],
                         cwd=str(tmp_path), env=os.environ.copy()) as channel:
        with pytest.raises(BridgeError) as error:
            channel.receive(.1)
        assert error.value.code == 'provider_timeout'
    assert channel.process.poll() is not None


def test_expired_queued_allow_reports_the_actual_delivered_denial(tmp_path):
    bridge = Bridge(tmp_path)
    bridge.register(Account('fixture', 'codex', home=str(tmp_path)))
    instance = bridge.instance_create(account_ref='fixture', workspace_path=str(tmp_path))
    turn, _ = bridge.store.admit('fixture-turn', instance['id'], 'fixture', RunOptions(), 'key')
    permissions = Permissions(bridge.store)
    request = permissions.request(turn, {'operation': 'fixture'}, timeout=30)
    permissions.respond(turn, request, 'allow')
    with bridge.store.connect() as db:
        db.execute('UPDATE permission_requests SET expires=0 WHERE id=?', (request,))
    applied = permissions.wait(turn, request)
    assert applied == 'deny'
    permissions.delivered(turn, request, applied)
    replay = permissions.respond(turn, request, 'allow')
    assert replay['state'] == 'delivered' and replay['applied_decision'] == 'deny'
    bridge.store.finish(turn, 'cancelled')
