"""`browser='isolated'`: a clean same-host login the host opens in a disposable profile.

AgentBridge records the entry and keeps the same-host wire request; the host process
launches `agentbridge.auth_browser`. A provider identity other than the expected email
fails the attempt and names the identity it saw, never binding it.
"""

import secrets

import pytest

from agentbridge import Bridge
from agentbridge.commands.parser import build_parser
from agentbridge.errors import BridgeError
from test_proxy_authentication import FakeProxyGrantBridge, proxy_responses, use_fake_grantbridge
from test_proxy_management import local_management


def login(bridge, port, **options):
    return bridge.account_login_start(
        provider='codex', name='Clean', proxy_base_url=f'http://127.0.0.1:{port}/v1',
        key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY', **options)


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    monkeypatch.setenv('LAB_PROXY_KEY', secrets.token_hex(16))


def test_isolated_login_keeps_the_same_host_wire_request_and_echoes_the_entry(
        tmp_path, monkeypatch, keys):
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            # The fake accepts positional arguments only: any entry field would fail here.
            use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(responses))
            started = login(bridge, port, browser='isolated', request_key='clean-1')
            assert (started['browser'], started['mode']) == ('isolated', 'browser')
            assert started['authorization_url'].startswith('https://')
            assert 'viewer_url' not in started
            replay = login(bridge, port, browser='isolated', request_key='clean-1')
            assert replay['attempt_id'] == started['attempt_id']
            with pytest.raises(BridgeError) as conflict:
                login(bridge, port, browser='same_host', request_key='clean-1')
            assert conflict.value.code == 'idempotency_conflict'
            owner = started['owner_ref']
            status = bridge.account_login_status(started['attempt_id'], owner_ref=owner)
            assert (status['status'], status['browser']) == ('authorized', 'isolated')
            bridge.account_login_check(started['attempt_id'], owner_ref=owner)
            result = bridge.account_login_complete(started['attempt_id'], owner_ref=owner)
            assert result['attempt']['status'] == 'bound'
            assert result['attempt']['browser'] == 'isolated'
            assert result['identity']['email'] == 'person@example.test'


def test_isolated_login_never_asks_for_a_hosted_browser(tmp_path, monkeypatch, keys):
    class Untouched:
        def __getattr__(self, name):
            raise AssertionError(f'GrantBridge must not be used: {name}')

    with local_management(proxy_responses()) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            use_fake_grantbridge(monkeypatch, Untouched())
            with pytest.raises(BridgeError) as error:
                login(bridge, port, browser='isolated', mode='hosted')
            assert error.value.code == 'unsupported_operation'
            assert bridge.account_login_attempts() == []


def test_identity_mismatch_fails_the_attempt_and_names_the_observed_identity(
        tmp_path, monkeypatch, keys):
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(responses))
            started = login(bridge, port, browser='isolated', email='expected@example.test')
            owner = started['owner_ref']
            with pytest.raises(BridgeError) as error:
                bridge.account_login_check(started['attempt_id'], owner_ref=owner)
            assert error.value.code == 'identity_changed'
            failed = bridge.account_login_status(started['attempt_id'], owner_ref=owner)
            assert failed['status'] == 'failed'
            assert failed['error'] == {'code': 'identity_changed'}
            assert failed['identity'] == {'email': 'person@example.test'}
            assert failed['verification'] == {'proxyBinding': 'failed'}
            assert 'expected@example.test' not in repr(failed['identity'])
            assert bridge.accounts() == []
            with pytest.raises(BridgeError) as rejected:
                bridge.account_login_complete(started['attempt_id'], owner_ref=owner)
            assert rejected.value.code == 'authentication_not_verified'


def test_cli_offers_the_isolated_choice_without_hosted():
    parser, _ = build_parser()
    args = parser.parse_args(['accounts', 'login-start', '--provider', 'claude', '--name', 'Clean',
                              '--browser', 'isolated'])
    assert (args.browser, args.mode) == ('isolated', 'browser')
    args = parser.parse_args(['accounts', 'login', '--provider', 'claude', '--name', 'Clean',
                              '--browser', 'isolated'])
    assert args.browser == 'isolated'
