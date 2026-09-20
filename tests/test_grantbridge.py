import pytest

from agentbridge import Account, Bridge, GrantBridgeClient


class FakeGrantBridge:
    def __init__(self, home):
        self.home = str(home)
        self.closed = False
        self.attempt = {
            'id': 'attempt-1', 'provider': 'codex', 'status': 'awaiting_user',
            'authorizationUrl': 'https://auth.openai.com/authorize',
        }

    def start(self, **_):
        return dict(self.attempt)

    def get(self, attempt_id, owner):
        assert attempt_id == 'attempt-1' and owner
        self.attempt['status'] = 'authorized'
        self.attempt['identity'] = {'email': 'ferran@example.test'}
        return dict(self.attempt)

    def check(self, attempt_id, owner):
        assert attempt_id == 'attempt-1' and owner
        self.attempt['status'] = 'authorized'
        self.attempt['verification'] = {'freshProcess': 'passed'}
        self.attempt['identity'] = {'email': 'ferran@example.test'}
        return dict(self.attempt)

    def activate(self, attempt_id, owner):
        assert attempt_id == 'attempt-1' and owner
        return {'attempt_id': 'attempt-1', 'provider': 'codex',
                'identity': {'email': 'ferran@example.test'}, 'home': self.home}

    def close(self):
        self.closed = True


def test_login_registers_only_safe_identity_and_native_home(tmp_path):
    state = tmp_path / 'state'
    native_home = tmp_path / 'profile' / '.codex'
    native_home.mkdir(parents=True)
    bridge = Bridge(state)
    fake = FakeGrantBridge(native_home)
    result = bridge.account_login(engine='codex', name='Development Codex', client=fake,
                                  poll_interval=0.05, timeout=2)
    assert result['account']['name'] == 'Development Codex'
    assert result['account']['home'] == str(native_home)
    assert result['identity'] == {'email': 'ferran@example.test'}
    assert bridge.account_status(result['account']['id'])['authentication']['status'] == 'usable'
    assert fake.closed
    assert result['attempt']['status'] == 'bound'
    assert 'secret' not in str(result)


def test_login_promotes_existing_account_without_changing_its_id(tmp_path):
    state = tmp_path / 'state'
    old_home = tmp_path / 'old'
    old_home.mkdir()
    native_home = tmp_path / 'profile' / '.codex'
    native_home.mkdir(parents=True)
    bridge = Bridge(state)
    bridge.register(Account('stable-id', 'codex', home=old_home,
                            name='Development Codex', email='ferran@example.test'))
    result = bridge.account_login(engine='codex', name='development codex', client=FakeGrantBridge(native_home),
                                  poll_interval=0.05, timeout=2)
    assert result['account']['id'] == 'stable-id'
    assert result['account']['home'] == str(native_home)
    assert bridge.resolve_account('Development Codex').id == 'stable-id'


def test_login_cannot_replace_an_existing_account_identity(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    bridge.register(Account('stable-id', 'codex', home=tmp_path / 'old',
                            name='Development Codex', email='other@example.test'))
    with pytest.raises(Exception) as error:
        bridge.account_login(engine='codex', name='Development Codex',
                             client=FakeGrantBridge(tmp_path / 'new'), poll_interval=0.05, timeout=2)
    assert error.value.code == 'identity_changed'
    assert bridge.account('stable-id').email == 'other@example.test'


@pytest.mark.parametrize('state', ['cancelled', 'expired', 'revoked', 'failed', 'interrupted'])
def test_terminal_authentication_status_overrides_previous_verification(state):
    from agentbridge.authentication import AuthenticationService
    assert AuthenticationService._status({'status': state, 'verification': {'freshProcess': 'passed'}}) == state


def test_async_login_attempt_is_durable_and_requires_fresh_check(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    native_home = tmp_path / 'profile' / '.codex'
    native_home.mkdir(parents=True)
    fake = FakeGrantBridge(native_home)
    started = bridge.account_login_start(engine='codex', name='Async Codex',
                                         request_key='login-once', client=fake)
    replay = bridge.account_login_start(engine='codex', name='Async Codex',
                                        request_key='login-once', client=fake)
    assert replay['attempt_id'] == started['attempt_id']
    assert started['status'] == 'awaiting_user'
    current = bridge.account_login_status(started['attempt_id'], owner_ref=started['owner_ref'], client=fake)
    assert current['status'] == 'authorized'
    checked = bridge.account_login_check(started['attempt_id'], owner_ref=started['owner_ref'], client=fake)
    assert checked['status'] == 'verified'
    completed = bridge.account_login_complete(started['attempt_id'], owner_ref=started['owner_ref'], client=fake)
    assert completed['attempt']['status'] == 'bound'
    assert bridge.resolve_account('Async Codex').home == str(native_home)


def test_grantbridge_client_drives_local_adapter_when_checkout_is_available(tmp_path):
    if not __import__('shutil').which('node'):
        pytest.skip('Node.js is required for the local GrantBridge adapter test.')
    adapter = __import__('pathlib').Path('/home/ferran/grantbridge/scripts/agentbridge-adapter.mjs')
    if not adapter.is_file():
        pytest.skip('The local GrantBridge checkout is not available.')
    client = GrantBridgeClient('/home/ferran/grantbridge', data_dir=tmp_path / 'grantbridge')
    try:
        assert client._request('health')['service'] == 'grantbridge'
        attempt = client.start(owner='adapter-test', engine='control', request_key='adapter-test')
        assert attempt['provider'] == 'control'
        current = client.get(attempt['id'], 'adapter-test')
        assert current['status'] == 'awaiting_user'
        assert client.cancel(attempt['id'], 'adapter-test')['status'] == 'cancelled'
    finally:
        client.close()
