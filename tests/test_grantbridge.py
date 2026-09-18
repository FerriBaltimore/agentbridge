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

    def activate(self, attempt_id, owner):
        assert attempt_id == 'attempt-1' and owner
        return {'provider': 'codex', 'identity': {'email': 'ferran@example.test'}, 'home': self.home}

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
    assert bridge.account_status(result['account']['id'])['authentication']['status'] == 'authenticated'
    assert fake.closed
    assert 'authorizationUrl' in result['attempt']
    assert 'secret' not in str(result)


def test_login_promotes_existing_account_without_changing_its_id(tmp_path):
    state = tmp_path / 'state'
    old_home = tmp_path / 'old'
    old_home.mkdir()
    native_home = tmp_path / 'profile' / '.codex'
    native_home.mkdir(parents=True)
    bridge = Bridge(state)
    bridge.register(Account('stable-id', 'codex', home=old_home,
                            name='Development Codex', email='old@example.test'))
    result = bridge.account_login(engine='codex', name='development codex', client=FakeGrantBridge(native_home),
                                  poll_interval=0.05, timeout=2)
    assert result['account']['id'] == 'stable-id'
    assert result['account']['home'] == str(native_home)
    assert bridge.resolve_account('Development Codex').id == 'stable-id'


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
