"""Managed account login keeps proxy infrastructure out of the public request."""

import os

from agentbridge import Bridge
from test_proxy_authentication import FakeProxyGrantBridge, proxy_responses, use_fake_grantbridge
from test_proxy_management import local_management


class FixtureManagedProxy:
    def __init__(self, port, monkeypatch):
        self.port = port
        self.monkeypatch = monkeypatch
        self.provisions = 0
        self.ensures = 0

    def _route(self, account_id):
        self.monkeypatch.setenv('LAB_PROXY_KEY', 'managed-fixture-client-secret')
        self.monkeypatch.setenv('LAB_MANAGEMENT_KEY', 'managed-fixture-management-secret')
        return {
            'proxy_base_url': f'http://127.0.0.1:{self.port}/v1',
            'key_env': 'LAB_PROXY_KEY',
            'management_key_env': 'LAB_MANAGEMENT_KEY',
        }

    def provision(self, account_id):
        self.provisions += 1
        return self._route(account_id)

    def ensure(self, account_id, base_url):
        self.ensures += 1
        result = self._route(account_id)
        assert result['proxy_base_url'] == base_url
        return result

    @staticmethod
    def is_managed(config, account_id):
        return config.get('key_env') == 'LAB_PROXY_KEY'


def test_provider_and_name_login_provisions_proxy_before_grantbridge(tmp_path, monkeypatch):
    responses = proxy_responses()
    with local_management(responses) as (port, _):
        with Bridge(tmp_path / 'state') as bridge:
            managed = FixtureManagedProxy(port, monkeypatch)
            bridge.managed_proxy = managed
            bridge.authentication.managed_proxy = managed
            bridge.routes.managed_proxy = managed
            use_fake_grantbridge(monkeypatch, FakeProxyGrantBridge(responses))

            first = bridge.account_login_start(
                provider='codex', name='Primary', request_key='managed-login')
            replay = bridge.account_login_start(
                provider='codex', name='Primary', request_key='managed-login')
            assert replay['attempt_id'] == first['attempt_id']
            assert managed.provisions == 1
            assert first['status'] == 'awaiting_user'

            assert bridge.account_login_status(
                first['attempt_id'], owner_ref=first['owner_ref'])['status'] == 'authorized'
            assert bridge.account_login_check(
                first['attempt_id'], owner_ref=first['owner_ref'])['status'] == 'verified'
            account = bridge.account_login_complete(
                first['attempt_id'], owner_ref=first['owner_ref'])['account']
            assert account['name'] == 'Primary'
            assert account['provider'] == 'codex'
            assert account['proxy_base_url'] == f'http://127.0.0.1:{port}/v1'
            assert managed.ensures >= 3

            monkeypatch.delenv('LAB_PROXY_KEY')
            monkeypatch.delenv('LAB_MANAGEMENT_KEY')
            assert bridge.models(refresh=True)['models'][0]['availability'] == 'proxy_observed'
            assert os.environ['LAB_PROXY_KEY'] == 'managed-fixture-client-secret'
            assert bridge.account_status(account_ref='Primary', refresh=True)['account_id'] == account['id']

            for path in (bridge.store.path, bridge.store.path.with_name('bridge.sqlite3-wal')):
                if path.exists():
                    contents = path.read_bytes()
                    assert b'managed-fixture-client-secret' not in contents
                    assert b'managed-fixture-management-secret' not in contents
