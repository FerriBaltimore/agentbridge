"""Owned OAuth abandonment stops real bundled sidecars without deleting credentials."""

import json
import socket

import pytest

from agentbridge import Bridge
from agentbridge.errors import BridgeError
from agentbridge.proxy.process_lifecycle import _record_process_running


class PendingGrantBridge:
    """Generate a deterministic OAuth URL without contacting an upstream provider."""

    def __init__(self, *args, **kwargs):
        pass

    def configuration(self):
        return {'adapter': 'fixture', 'data_dir': None, 'node': 'fixture'}

    def proxy_start(self, provider, base_url, management_key_env):
        return {'id': 'fixture-new-state', 'provider': provider, 'status': 'awaiting_user',
                'authorizationUrl': 'https://auth.openai.com/authorize?state=fixture-new-state'}

    def close(self):
        pass


def listening(port):
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(('127.0.0.1', port)) == 0


@pytest.mark.parametrize('status', ['interrupted', 'abandoned', 'failed', 'expired', 'cancelled'])
def test_owned_terminal_login_stops_only_its_sidecar_and_next_start_gets_new_route(
    tmp_path, monkeypatch, status,
):
    monkeypatch.delenv('AGENTBRIDGE_CLIPROXY_BIN', raising=False)
    monkeypatch.setattr('agentbridge.authentication.GrantBridgeClient', PendingGrantBridge)
    with Bridge(tmp_path / 'state') as bridge:
        managed = bridge.managed_proxy
        try:
            route = managed.provision('fixture-original')
            other = managed.provision('fixture-other')
            directory = managed.directory / 'accounts' / 'fixture-original'
            record = json.loads((directory / 'route.json').read_text())
            other_record = json.loads(
                (managed.directory / 'accounts' / 'fixture-other' / 'route.json').read_text())
            marker = directory / 'auth' / 'fixture-preserved.txt'
            marker.write_text('Synthetic credential marker; never an upstream credential.')
            attempt = {'id': 'fixture-login', 'owner': 'fixture-owner',
                'account_id': 'fixture-original', 'engine': 'codex', 'name': 'Fixture',
                'mode': 'browser', 'browser': 'same_host', 'status': status,
                'grantbridge_id': 'fixture-original-state',
                'data': {'error': {'code': 'authentication_outcome_unknown'}}}
            bridge.store.create_auth_attempt(attempt, connection={}, proxy_route=route)
            with pytest.raises(BridgeError) as foreign:
                bridge.account_login_cancel('fixture-login', owner_ref='foreign-owner')
            assert foreign.value.code == 'authentication_attempt_not_found'
            assert listening(record['port']) and _record_process_running(record)
            cancelled = bridge.account_login_cancel('fixture-login', owner_ref='fixture-owner')
            expected = 'abandoned' if status == 'interrupted' else status
            assert cancelled['status'] == expected
            assert cancelled['error'] == {'code': 'authentication_outcome_unknown'}
            assert not listening(record['port']) and not _record_process_running(record)
            assert listening(other_record['port']) and _record_process_running(other_record)
            assert marker.read_text().startswith('Synthetic credential marker;')
            assert bridge.account_login_cancel(
                'fixture-login', owner_ref='fixture-owner') == cancelled
            assert bridge.account_login_status(
                'fixture-login', owner_ref='fixture-owner') == cancelled
            started = bridge.account_login_start(
                provider='codex', name='Fixture', owner_ref='fixture-owner', request_key='fresh')
            assert started['status'] == 'awaiting_user' and started['authorization_url']
            saved = bridge.store.get_auth_attempt(started['attempt_id'], 'fixture-owner')
            fresh = bridge.store.auth_proxy_route(started['attempt_id'])['config']
            assert saved['account_id'] not in {'fixture-original', 'fixture-other'}
            assert fresh['proxy_base_url'] not in {route['proxy_base_url'], other['proxy_base_url']}
            assert bridge.accounts() == []
        finally:
            managed.shutdown()
