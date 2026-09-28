"""A phone completes the GrantBridge proxy login without dialling the loopback ports.

Contract: docs/interface/mobile-login.md. GrantBridge is faked at the client boundary;
the CLIProxyAPI Management API is the same local fixture the desktop tests use.
"""

import secrets
import shutil
from pathlib import Path

import pytest

import agentbridge
from agentbridge import Bridge, GrantBridgeClient
from agentbridge import auth_contract
from agentbridge.commands.parser import build_parser
from agentbridge.errors import BridgeError
from test_proxy_authentication import FakeProxyGrantBridge, proxy_responses, use_fake_grantbridge
from test_proxy_management import local_management

PHONE_OWNER = 'f' * 64  # sha256 digest of the phone's GrantBridge session, supplied as owner_ref
VIEWER = 'https://broker.example/browser.html?id=7'


class FakeMobileGrantBridge(FakeProxyGrantBridge):
    """Echoes the phone entry like GrantBridge's ProxyLogins and records what it received."""

    def __init__(self, responses, provider='codex', echo=True, viewer=VIEWER, row_error=None):
        super().__init__(responses, provider)
        self.echo, self.viewer, self.row_error = echo, viewer, row_error
        self.starts, self.cancelled = [], []

    def proxy_start(self, provider, base_url, management_key_env, **entry):
        self.starts.append(entry)
        result = super().proxy_start(provider, base_url, management_key_env)
        if entry and self.echo:
            result.update({'browser': entry['browser'], 'mode': entry['mode']})
            if entry['mode'] == 'hosted':
                result['viewerUrl'] = self.viewer
                result['hostedBrowser'] = {'path': '/browser.html?id=7', 'status': 'starting'}
        return result

    def proxy_status(self, state, provider, base_url, management_key_env):
        if self.row_error:
            return {'id': state, 'provider': provider, 'status': 'failed',
                    'error': {'code': self.row_error, 'message': 'private detail'}}
        return super().proxy_status(state, provider, base_url, management_key_env)

    def proxy_cancel(self, state, provider, base_url, management_key_env):
        self.cancelled.append(state)
        return super().proxy_cancel(state, provider, base_url, management_key_env)


def login(bridge, port, **options):
    return bridge.account_login_start(
        provider='codex', name='Phone', proxy_base_url=f'http://127.0.0.1:{port}/v1',
        key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY', **options)


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    monkeypatch.setenv('LAB_PROXY_KEY', secrets.token_hex(16))


def test_phone_browser_login_binds_its_owner_and_returns_through_the_callback(tmp_path, monkeypatch, keys):
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            fake = FakeMobileGrantBridge(responses)
            use_fake_grantbridge(monkeypatch, fake)
            started = login(bridge, port, browser='mobile', mode='browser', owner_ref=PHONE_OWNER)
            assert fake.starts == [{'browser': 'mobile', 'mode': 'browser', 'owner': PHONE_OWNER}]
            assert (started['browser'], started['mode']) == ('mobile', 'browser')
            assert started['authorization_url'].startswith('https://')
            assert 'viewer_url' not in started
            url = 'http://localhost:1455/auth/callback?code=private-code&state=oauth-state'
            delivered = bridge.account_login_callback(started['attempt_id'],
                                                      owner_ref=PHONE_OWNER, redirect_url=url)
            assert delivered['status'] == 'awaiting_user'
            assert bridge.account_login_status(
                started['attempt_id'], owner_ref=PHONE_OWNER)['status'] == 'authorized'
            bridge.account_login_check(started['attempt_id'], owner_ref=PHONE_OWNER)
            result = bridge.account_login_complete(started['attempt_id'], owner_ref=PHONE_OWNER)
            assert result['attempt']['status'] == 'bound'
            assert result['attempt']['browser'] == 'mobile'
            saved = bridge.store.get_auth_attempt(started['attempt_id'], PHONE_OWNER)
            assert 'private-code' not in repr(saved) and 'LAB_MANAGEMENT_KEY' not in repr(started)


def test_hosted_login_exposes_only_the_viewer_and_reports_browser_failures(tmp_path, monkeypatch, keys):
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            fake = FakeMobileGrantBridge(responses, row_error='browser_busy')
            use_fake_grantbridge(monkeypatch, fake)
            started = login(bridge, port, browser='mobile', mode='hosted', owner_ref=PHONE_OWNER)
            assert fake.starts[0]['mode'] == 'hosted'
            assert started['viewer_url'] == VIEWER
            assert started['mode'] == 'hosted'
            assert 'authorization_url' not in started
            saved = bridge.store.get_auth_attempt(started['attempt_id'], PHONE_OWNER)
            assert saved['data']['authorizationUrl'].startswith('https://')
            failed = bridge.account_login_status(started['attempt_id'], owner_ref=PHONE_OWNER)
            assert (failed['status'], failed['error']) == ('failed', {'code': 'browser_busy'})
            assert 'private detail' not in repr(failed)


def test_desktop_request_and_projection_are_unchanged(tmp_path, monkeypatch, keys):
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(responses))  # positional-only start
            started = login(bridge, port)
            assert (started['browser'], started['mode']) == ('same_host', 'browser')
            assert started['authorization_url'].startswith('https://')


@pytest.mark.parametrize('entry, code', [
    ({'browser': 'remote_desktop'}, 'invalid_request'),
    ({'browser': 'mobile', 'mode': 'device'}, 'invalid_request'),
    ({'browser': 'same_host', 'mode': 'hosted'}, 'unsupported_operation'),
])
def test_arbitrary_entries_are_rejected_before_grantbridge(tmp_path, monkeypatch, keys, entry, code):
    class Untouched:
        def __getattr__(self, name):
            raise AssertionError(f'GrantBridge must not be used: {name}')

    with local_management(proxy_responses()) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            use_fake_grantbridge(monkeypatch, Untouched())
            with pytest.raises(BridgeError) as error:
                login(bridge, port, **entry)
            assert error.value.code == code
            assert bridge.account_login_attempts() == []


def test_hosted_browser_unavailable_fails_the_attempt_before_dispatch(tmp_path, monkeypatch, keys):
    class NoHost(FakeMobileGrantBridge):
        def proxy_start(self, provider, base_url, management_key_env, **entry):
            raise BridgeError('hosted_browser_unavailable', 'stdio adapter')

    with local_management(proxy_responses()) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            use_fake_grantbridge(monkeypatch, NoHost(proxy_responses()))
            with pytest.raises(BridgeError) as error:
                login(bridge, port, browser='mobile', mode='hosted', owner_ref=PHONE_OWNER,
                      request_key='phone-1')
            assert error.value.code == 'hosted_browser_unavailable'
            saved = bridge.store.auth_attempt_for_request(PHONE_OWNER, 'phone-1')
            assert saved['status'] == 'failed'
            assert saved['data']['error'] == {'code': 'hosted_browser_unavailable'}


@pytest.mark.parametrize('fake_options, mode', [
    ({'echo': False}, 'browser'),  # an older GrantBridge that ignores the phone entry
    ({'viewer': 'http://broker.example/browser.html'}, 'hosted'),  # a page the phone must not open
])
def test_grantbridge_without_the_phone_entry_is_rejected_and_its_session_released(
        tmp_path, monkeypatch, keys, fake_options, mode):
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            fake = FakeMobileGrantBridge(responses, **fake_options)
            use_fake_grantbridge(monkeypatch, fake)
            with pytest.raises(BridgeError) as error:
                login(bridge, port, browser='mobile', mode=mode, owner_ref=PHONE_OWNER,
                      request_key='phone-2')
            assert error.value.code == 'provider_protocol_error'
            assert fake.cancelled == ['oauth-state']
            saved = bridge.store.auth_attempt_for_request(PHONE_OWNER, 'phone-2')
            assert saved['status'] == 'failed'
            assert saved['grantbridge_id'] is None
            assert bridge.accounts() == []


def test_client_sends_the_phone_entry_only_for_phones(monkeypatch):
    sent = []
    client = GrantBridgeClient.__new__(GrantBridgeClient)
    monkeypatch.setattr(client, '_proxy_key', lambda name: 'secret-value', raising=False)
    monkeypatch.setattr(client, '_request', lambda method, params: sent.append((method, params)),
                        raising=False)
    monkeypatch.setattr('agentbridge.grantbridge.ensure_callback_port_available', lambda provider: None)
    client.proxy_start('codex', 'http://127.0.0.1:9/v1', 'KEY')
    client.proxy_start('claude', 'http://127.0.0.1:9/v1', 'KEY', browser='mobile', mode='hosted',
                       owner=PHONE_OWNER)
    assert sent[0][1] == {'provider': 'codex', 'base_url': 'http://127.0.0.1:9/v1',
                          'management_key': 'secret-value'}
    assert sent[1][1] == {'provider': 'claude', 'base_url': 'http://127.0.0.1:9/v1',
                          'management_key': 'secret-value', 'browser': 'mobile', 'mode': 'hosted',
                          'owner': PHONE_OWNER}


def test_bundled_adapter_answers_the_phone_entry_without_a_host(monkeypatch):
    """The shipped stdio adapter echoes a phone browser login and refuses a hosted one."""
    if not shutil.which('node'):
        pytest.skip('Node.js is required for the bundled GrantBridge adapter.')
    monkeypatch.setattr('agentbridge.grantbridge.ensure_callback_port_available', lambda provider: None)
    bundled = Path(agentbridge.__file__).resolve().parent / 'bundle' / 'grantbridge'
    secret = secrets.token_hex(16)
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secret)
    responses = {'/v0/management/anthropic-auth-url?is_webui=true': (
        200, {'status': 'ok', 'url': 'https://claude.ai/oauth/authorize?fixture=1',
              'state': 'fixture-oauth-state'}, {})}
    with local_management(responses) as (port, seen):
        base_url = f'http://127.0.0.1:{port}/v1'
        with GrantBridgeClient(bundled) as grantbridge:
            started = grantbridge.proxy_start('claude', base_url, 'LAB_MANAGEMENT_KEY',
                                              browser='mobile', mode='browser', owner=PHONE_OWNER)
            with pytest.raises(BridgeError) as hosted:
                grantbridge.proxy_start('claude', base_url, 'LAB_MANAGEMENT_KEY',
                                        browser='mobile', mode='hosted', owner=PHONE_OWNER)
    assert started == {'id': 'fixture-oauth-state', 'provider': 'claude', 'status': 'awaiting_user',
                       'authorizationUrl': 'https://claude.ai/oauth/authorize?fixture=1',
                       'browser': 'mobile', 'mode': 'browser'}
    assert hosted.value.code == 'hosted_browser_unavailable'
    assert len(seen) == 1, 'the hosted refusal never reaches the sidecar'
    assert secret not in repr(started) + repr(hosted.value)


def test_viewer_url_projection_accepts_https_or_loopback_only():
    base = {'id': 'x', 'provider': 'codex', 'status': 'awaiting_user'}
    assert auth_contract.projection({**base, 'viewerUrl': VIEWER})['viewerUrl'] == VIEWER
    local = 'http://127.0.0.1:8787/browser.html?id=7'
    assert auth_contract.projection({**base, 'viewerUrl': local})['viewerUrl'] == local
    for bad in ('http://broker.example/browser.html', 'https://user:pw@broker.example/',
                'javascript:alert(1)', 'https://' + 'a' * 2048, 7):
        assert 'viewerUrl' not in auth_contract.projection({**base, 'viewerUrl': bad})


def test_cli_offers_mobile_and_hosted_choices():
    parser, _ = build_parser()
    args = parser.parse_args(['accounts', 'login-start', '--provider', 'codex', '--name', 'Phone',
                              '--browser', 'mobile', '--mode', 'hosted'])
    assert (args.browser, args.mode) == ('mobile', 'hosted')
    with pytest.raises(SystemExit):
        parser.parse_args(['accounts', 'login-start', '--provider', 'codex', '--name', 'Phone',
                           '--browser', 'remote_desktop'])
