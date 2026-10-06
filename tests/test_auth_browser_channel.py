"""AA-AUTH-SDK: owned browser operations preserve verification, admission and recovery."""

import secrets

import pytest

from agentbridge import Bridge
from agentbridge.errors import BridgeError
from agentbridge.rpc import dispatch
from agentbridge.proxy.credential_barrier import initialize
from test_mobile_login import FakeMobileGrantBridge, login
from test_proxy_authentication import proxy_responses, use_fake_grantbridge
from test_proxy_management import local_management


class BrowserGrantBridge(FakeMobileGrantBridge):
    def __init__(self, responses):
        super().__init__(responses, viewer='browser.html')
        self.calls = []
        self.ready_to_verify = False
        # What GrantBridge rc.21 answers to `stream`: a sandbox socket path and a fresh token.
        self.stream = {'socket': '/tmp/grantbridge-stream-x1/oauth-state.sock',
                       'token': secrets.token_hex(32), 'expires_at': 1_900_000_030_000}

    def proxy_start(self, *args, **kwargs):
        result = super().proxy_start(*args, **kwargs)
        result['browserTransport'] = 'rpc'
        return result

    def proxy_status(self, state, provider, *args):
        return {'id': state, 'provider': provider,
                'status': 'authorized' if self.ready_to_verify else 'awaiting_user'}

    def browser(self, attempt_id, owner, **options):
        self.calls.append((attempt_id, owner, options))
        if options['action'] == 'asset':
            return {'content_type': 'text/html; charset=utf-8', 'body': '<html></html>'}
        if options['action'] == 'input':
            return {'editable': True}
        if options['action'] == 'stream':
            return dict(self.stream)
        return {'status': 'awaiting_user', 'ready': True, 'done': False, 'sequence': 1,
                'origin': 'https://auth.example.test',
                'viewport': {'width': 1280, 'height': 800, 'scale': 2}}


def test_aa_auth_sdk_browser_lifetime_admission_verification_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    monkeypatch.setenv('LAB_PROXY_KEY', secrets.token_hex(16))
    responses = proxy_responses()
    fake = BrowserGrantBridge(responses)
    use_fake_grantbridge(monkeypatch, fake)
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            start = login(bridge, port, browser='mobile', mode='hosted',
                          owner_ref='owner-a', request_key='browser-login')
            assert start['viewer_url'] == 'browser.html'
            assert 'authorization_url' not in start
            assert fake.closed is False
            params = {'attempt_id': start['attempt_id'], 'owner_ref': 'owner-a'}
            assert login(bridge, port, browser='mobile', mode='hosted',
                         owner_ref='owner-a', request_key='browser-login') == start
            assert len(fake.starts) == 1
            view = dispatch(bridge, 'accounts.login.browser', params)
            assert view['ready']
            # GrantBridge's viewport, including the device scale, reaches the viewer untouched.
            assert view['viewport'] == {'width': 1280, 'height': 800, 'scale': 2}
            with pytest.raises(BridgeError):
                dispatch(bridge, 'accounts.login.browser', {**params, 'owner_ref': 'owner-b'})
            with pytest.raises(BridgeError):
                dispatch(bridge, 'accounts.login.browser', {**params, 'action': 'asset',
                                                           'asset': '../secrets'})
            row = bridge.store.get_auth_attempt(start['attempt_id'], 'owner-a')
            with bridge.store.connect() as db:
                initialize(db)
                db.execute('INSERT INTO credential_account_holds VALUES (?,?,?)',
                           (row['account_id'], 'fixture-capture', 'capture'))
            before = len(fake.calls)
            with pytest.raises(BridgeError, match='held'):
                dispatch(bridge, 'accounts.login.browser', {**params, 'action': 'input',
                                                           'input': {'type': 'text', 'text': 'x'}})
            assert len(fake.calls) == before
            with bridge.store.connect() as db:
                db.execute('DELETE FROM credential_account_holds')
            assert dispatch(bridge, 'accounts.login.browser', {
                **params, 'action': 'input', 'input': {'type': 'text', 'text': 'private-text'}})
            assert 'private-text' not in repr(bridge.store.get_auth_attempt(
                start['attempt_id'], 'owner-a'))
            fake.ready_to_verify = True
            bridge.account_login_check(**params)
            assert bridge.account_login_complete(**params)['attempt']['status'] == 'bound'
        assert fake.closed is True

        fake = BrowserGrantBridge(responses)
        use_fake_grantbridge(monkeypatch, fake)
        with Bridge(tmp_path / 'restart') as bridge:
            responses['/v0/management/auth-files'] = (200, {'files': []}, {})
            start = login(bridge, port, browser='mobile', mode='hosted', owner_ref='owner-a')
        with Bridge(tmp_path / 'restart') as recovered:
            result = recovered.account_login_status(start['attempt_id'], owner_ref='owner-a')
            assert result['status'] == 'interrupted'
            ended = recovered.account_login_browser(start['attempt_id'], owner_ref='owner-a')
            assert ended['done']
            assert ended['viewport'] == {'width': 390, 'height': 760, 'scale': 1}


def test_aa_auth_dead_browser_drains_without_reopening_and_close_continues(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from agentbridge.proxy.credential_barrier import require_drained

    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    monkeypatch.setenv('LAB_PROXY_KEY', secrets.token_hex(16))
    responses = proxy_responses()
    fake = BrowserGrantBridge(responses)
    fake.alive = True
    use_fake_grantbridge(monkeypatch, fake)
    with local_management(responses) as (port, _):
        bridge = Bridge(tmp_path / 'state')
        start = login(bridge, port, browser='mobile', mode='hosted', owner_ref='owner-a')
        fake.alive = False
        result = bridge.account_login_status(start['attempt_id'], owner_ref='owner-a')
        assert result['status'] == 'interrupted'
        assert len(fake.starts) == 1
        row = bridge.store.get_auth_attempt(start['attempt_id'], 'owner-a')
        with bridge.store.connect() as db:
            require_drained(db, [row['account_id']])
        assert not bridge.authentication._grantbridge_clients

        # A broken child close must not abandon other clients or Bridge child reaping.
        closed = []
        def broken_close():
            closed.append('broken')
            raise OSError('synthetic close failure')
        bridge.authentication._grantbridge_clients.update({
            'broken': SimpleNamespace(close=broken_close),
            'remaining': SimpleNamespace(close=lambda: closed.append('remaining')),
        })
        bridge._children['completed-fixture'] = SimpleNamespace(
            poll=lambda: 0, wait=lambda: closed.append('reaped'))
        bridge.close()
        assert closed == ['broken', 'remaining', 'reaped']
        assert not bridge._children
        bridge.authentication.close = broken_close
        bridge._children['second-fixture'] = SimpleNamespace(
            poll=lambda: 0, wait=lambda: closed.append('finally-reaped'))
        with pytest.raises(OSError):
            bridge.close()
        assert closed[-1] == 'finally-reaped'


def test_aa_auth_grantbridge_failed_interrupts_pending_browser(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    monkeypatch.setenv('LAB_PROXY_KEY', secrets.token_hex(16))
    responses = proxy_responses()
    fake = BrowserGrantBridge(responses)
    use_fake_grantbridge(monkeypatch, fake)
    with local_management(responses) as (port, _), Bridge(tmp_path / 'state') as bridge:
        start = login(bridge, port, browser='mobile', mode='hosted', owner_ref='owner-a')
        def failed(*_args):
            raise BridgeError('grantbridge_failed', 'Synthetic child loss.')
        fake.proxy_status = failed
        result = bridge.account_login_status(start['attempt_id'], owner_ref='owner-a')
        assert result['status'] == 'interrupted'
        assert result['error']['code'] == 'authentication_outcome_unknown'
        assert len(fake.starts) == 1


def hosted_login(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(16))
    monkeypatch.setenv('LAB_PROXY_KEY', secrets.token_hex(16))
    responses = proxy_responses()
    fake = BrowserGrantBridge(responses)
    use_fake_grantbridge(monkeypatch, fake)
    return responses, fake


def test_av03_stream_is_relayed_live_and_refused_once_the_login_has_ended(tmp_path, monkeypatch):
    responses, fake = hosted_login(tmp_path, monkeypatch)
    with local_management(responses) as (port, _), Bridge(tmp_path / 'state') as bridge:
        start = login(bridge, port, browser='mobile', mode='hosted', owner_ref='owner-a')
        params = {'attempt_id': start['attempt_id'], 'owner_ref': 'owner-a'}
        stream = dispatch(bridge, 'accounts.login.browser', {**params, 'action': 'stream'})
        # GrantBridge's socket path, token and expiry reach the caller untouched.
        assert stream == fake.stream
        grantbridge_id, _, options = fake.calls[-1]
        assert (grantbridge_id, options['action']) == ('oauth-state', 'stream')
        assert options.get('input') is None and options.get('asset') is None
        # The token is a live secret: the attempt record never retains it.
        row = bridge.store.get_auth_attempt(start['attempt_id'], 'owner-a')
        assert fake.stream['token'] not in repr(row)
        with pytest.raises(BridgeError):
            dispatch(bridge, 'accounts.login.browser',
                     {**params, 'owner_ref': 'owner-b', 'action': 'stream'})
        view = dispatch(bridge, 'accounts.login.browser', params)
        assert view['ready'] and 'token' not in view and 'socket' not in view
        assert dispatch(bridge, 'accounts.login.browser', {**params, 'action': 'input',
                                                          'input': {'type': 'key', 'key': 'Tab'}})
        bridge.account_login_cancel(**params)
        row = bridge.store.get_auth_attempt(start['attempt_id'], 'owner-a')
        assert row['status'] == 'cancelled'
        relayed = len(fake.calls)
        # An ended login refuses `stream` like `input`; only `view` answers its placeholder.
        with pytest.raises(BridgeError) as error:
            dispatch(bridge, 'accounts.login.browser', {**params, 'action': 'stream'})
        assert error.value.code == 'authentication_attempt_not_ready'
        with pytest.raises(BridgeError) as error:
            dispatch(bridge, 'accounts.login.browser', {**params, 'action': 'input',
                                                       'input': {'type': 'key', 'key': 'Tab'}})
        assert error.value.code == 'authentication_attempt_not_ready'
        ended = dispatch(bridge, 'accounts.login.browser', params)
        assert ended['done'] and ended['viewport'] == {'width': 390, 'height': 760, 'scale': 1}
        assert len(fake.calls) == relayed


def test_av03_stream_with_a_lost_client_reports_unknown_outcome_like_view(tmp_path, monkeypatch):
    responses, fake = hosted_login(tmp_path, monkeypatch)
    with local_management(responses) as (port, _), Bridge(tmp_path / 'state') as bridge:
        start = login(bridge, port, browser='mobile', mode='hosted', owner_ref='owner-a')
        def failed(*_args, **_options):
            raise BridgeError('grantbridge_failed', 'Synthetic child loss.')
        fake.browser = failed
        with pytest.raises(BridgeError) as error:
            bridge.account_login_browser(start['attempt_id'], owner_ref='owner-a',
                                         action='stream')
        assert error.value.code == 'authentication_outcome_unknown'
        assert fake.closed is True
        assert not bridge.authentication._grantbridge_clients
        row = bridge.store.get_auth_attempt(start['attempt_id'], 'owner-a')
        assert (row['status'], row['data']['error']['code']) == (
            'interrupted', 'authentication_outcome_unknown')
        # The interrupted login is terminal: a later `stream` is refused without a new child.
        with pytest.raises(BridgeError) as error:
            bridge.account_login_browser(start['attempt_id'], owner_ref='owner-a',
                                         action='stream')
        assert error.value.code == 'authentication_attempt_not_ready'
        assert len(fake.starts) == 1


def test_av03_stream_requires_the_hosted_rpc_browser(tmp_path, monkeypatch):
    responses, fake = hosted_login(tmp_path, monkeypatch)
    with local_management(responses) as (port, _), Bridge(tmp_path / 'state') as bridge:
        start = login(bridge, port, browser='mobile', mode='browser', owner_ref='owner-a')
        with pytest.raises(BridgeError) as error:
            bridge.account_login_browser(start['attempt_id'], owner_ref='owner-a',
                                         action='stream')
        assert error.value.code == 'unsupported_operation'
        assert fake.calls == []
