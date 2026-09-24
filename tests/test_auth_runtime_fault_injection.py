"""Proxy login failures stay durable, sanitized, and idempotent."""

import json
import secrets

import pytest

from agentbridge import Bridge
from agentbridge.errors import BridgeError
from agentbridge.rpc import dispatch
from test_proxy_management import EMPTY_CONFIG, local_management


class FailingGrantBridge:
    starts = 0

    def proxy_start(self, provider, base_url, management_key_env):
        self.starts += 1
        raise BridgeError('grantbridge_failed', 'PRIVATE PROVIDER ERROR BODY')

    def close(self):
        pass

    def configuration(self):
        return {'adapter': 'fixture', 'data_dir': None, 'node': 'fixture'}


class BusyCallbackGrantBridge(FailingGrantBridge):
    def proxy_start(self, provider, base_url, management_key_env):
        self.starts += 1
        raise BridgeError('oauth_callback_port_busy', 'Local OAuth callback port is in use.')


def test_unknown_proxy_start_persists_only_safe_code_and_needs_explicit_cancel(tmp_path, monkeypatch):
    management_key = secrets.token_hex(24)
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', management_key)
    responses = {'/v0/management/auth-files': (200, {'files': []}, {}),
                 '/v0/management/config': (200, EMPTY_CONFIG, {})}
    with local_management(responses) as (port, _):
        client = FailingGrantBridge()
        monkeypatch.setattr('agentbridge.authentication.GrantBridgeClient',
                            lambda *args, **kwargs: client)
        with Bridge(tmp_path / 'state') as bridge:
            options = {'provider': 'codex', 'name': 'Fixture',
                       'proxy_base_url': f'http://127.0.0.1:{port}/v1',
                       'key_env': 'LAB_PROXY_KEY', 'management_key_env': 'LAB_MANAGEMENT_KEY',
                       'owner_ref': 'fixture-owner', 'request_key': 'once'}
            with pytest.raises(BridgeError) as error:
                bridge.account_login_start(**options)
            assert error.value.code == 'authentication_outcome_unknown'
            assert error.value.safe_data()['outcome'] == 'unknown'
            assert error.value.details['owner_ref'] == 'fixture-owner'
            assert len(error.value.details['attempt_id']) == 32
            replay = bridge.account_login_start(**options)
            assert replay['attempt_id'] == error.value.details['attempt_id']
            assert replay['status'] == 'interrupted'
            assert replay['error'] == {'code': 'authentication_outcome_unknown'}
            assert client.starts == 1
            assert bridge.accounts() == []
            with pytest.raises(BridgeError) as busy:
                bridge.account_login_start(**{**options, 'request_key': 'new-attempt'})
            assert busy.value.code == 'busy'
            abandoned = bridge.account_login_cancel(replay['attempt_id'], owner_ref=replay['owner_ref'])
            assert abandoned['status'] == 'abandoned'
            with bridge.store.connect() as db:
                rows = db.execute('SELECT data FROM auth_attempts').fetchall()
            assert len(rows) == 1
            assert json.loads(rows[0]['data']) == {'error': {'code': 'authentication_outcome_unknown'}}
            assert management_key not in str(rows) + repr(replay)
            assert 'PRIVATE PROVIDER ERROR BODY' not in str(rows) + repr(replay)


def test_known_callback_conflict_does_not_become_unknown_or_retire_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(24))
    responses = {'/v0/management/auth-files': (200, {'files': []}, {}),
                 '/v0/management/config': (200, EMPTY_CONFIG, {})}
    with local_management(responses) as (port, _):
        client = BusyCallbackGrantBridge()
        monkeypatch.setattr('agentbridge.authentication.GrantBridgeClient',
                            lambda *args, **kwargs: client)
        with Bridge(tmp_path / 'state') as bridge:
            options = {'provider': 'codex', 'name': 'Fixture',
                       'proxy_base_url': f'http://127.0.0.1:{port}/v1',
                       'key_env': 'LAB_PROXY_KEY',
                       'management_key_env': 'LAB_MANAGEMENT_KEY',
                       'owner_ref': 'fixture-owner', 'request_key': 'first'}
            with pytest.raises(BridgeError) as error:
                bridge.account_login_start(**options)
            assert error.value.code == 'oauth_callback_port_busy'
            assert error.value.safe_data()['category'] == 'auth'
            assert error.value.safe_data()['action'] == 'login'
            replay = bridge.account_login_start(**options)
            assert replay['status'] == 'failed'
            assert replay['error'] == {'code': 'oauth_callback_port_busy'}
            assert client.starts == 1
            with pytest.raises(BridgeError) as retry:
                bridge.account_login_start(**{**options, 'request_key': 'second'})
            assert retry.value.code == 'oauth_callback_port_busy'
            assert client.starts == 2


def test_orphaned_unknown_attempt_is_recoverable_only_by_explicit_abandon(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(24))
    responses = {'/v0/management/auth-files': (200, {'files': []}, {}),
                 '/v0/management/config': (200, EMPTY_CONFIG, {})}
    with local_management(responses) as (port, _):
        client = FailingGrantBridge()
        monkeypatch.setattr('agentbridge.authentication.GrantBridgeClient',
                            lambda *args, **kwargs: client)
        root = tmp_path / 'state'
        with Bridge(root) as bridge:
            with pytest.raises(BridgeError):
                bridge.account_login_start(
                    provider='codex', name='Orphaned',
                    proxy_base_url=f'http://127.0.0.1:{port}/v1',
                    key_env='LAB_PROXY_KEY', management_key_env='LAB_MANAGEMENT_KEY')
        with Bridge(root) as resumed:
            attempts = dispatch(resumed, 'accounts.login.list', {})
            assert len(attempts) == 1
            attempt = attempts[0]
            assert attempt['provider'] == 'codex'
            assert attempt['account_ref'] == 'Orphaned'
            assert attempt['error'] == {'code': 'authentication_outcome_unknown'}
            assert len(attempt['attempt_id']) == len(attempt['owner_ref']) == 32
            assert 'PRIVATE PROVIDER ERROR BODY' not in repr(attempts)
            assert resumed.account_login_attempts(provider='claude') == []
            assert resumed.account_login_attempts(limit=1) == attempts
            assert client.starts == 1
            abandoned = resumed.account_login_cancel(
                attempt['attempt_id'], owner_ref=attempt['owner_ref'])
            assert abandoned['status'] == 'abandoned'
            assert resumed.account_login_attempts() == []
            assert client.starts == 1
