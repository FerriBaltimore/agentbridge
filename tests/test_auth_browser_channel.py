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
        return {'status': 'awaiting_user', 'ready': True, 'done': False, 'sequence': 1}


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
            assert dispatch(bridge, 'accounts.login.browser', params)['ready']
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
            assert recovered.account_login_browser(start['attempt_id'],
                                                   owner_ref='owner-a')['done']


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
