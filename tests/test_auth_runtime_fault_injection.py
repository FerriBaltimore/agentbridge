"""Proxy login failures stay durable, sanitized, and idempotent."""

import json
import secrets

import pytest

from agentbridge import Bridge
from agentbridge.errors import BridgeError
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
            replay = bridge.account_login_start(**options)
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
