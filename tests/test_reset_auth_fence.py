"""Account login activation and reset admission cannot overtake each other."""

import json
import time

import pytest

from agentbridge.auth_proxy_binding import bind_proxy_account
from agentbridge.errors import BridgeError
from test_account_resets import FIRST_KEY, bound_bridge, observed, provider_calls
from test_proxy_reset_credits import reset_proxy


def _attempt(bridge, status):
    account = bridge.resolve_account('fixture')
    attempt = {
        'id': 'reauth-fixture', 'owner': 'fixture-owner', 'account_id': account.id,
        'engine': 'codex', 'name': account.name, 'mode': 'browser',
        'browser': 'same_host', 'status': status, 'data': {},
    }
    route = {key: getattr(account, key) for key in
             ('proxy_base_url', 'key_env', 'management_key_env')}
    return attempt, route


@pytest.mark.parametrize('status', ['awaiting_user', 'verified', 'interrupted'])
def test_fresh_reset_rejects_active_or_uncertain_login(tmp_path, monkeypatch, status):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        attempt, route = _attempt(bridge, status)
        bridge.store.create_auth_attempt(attempt, proxy_route=route)

        with pytest.raises(BridgeError) as error:
            bridge.account_quota_reset('fixture', idempotency_key=FIRST_KEY,
                                       observation_ref=snapshot['observation_ref'])
        assert error.value.code == 'authentication_in_progress'
        assert bridge.store.pending_reset_attempt('fixture') is None
        assert provider_calls(state, 'POST') == []


def test_pending_reset_transactionally_blocks_late_login_activation(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        binding = bridge.store.proxy_binding('fixture')
        bridge.store.begin_reset_attempt(
            'fixture', FIRST_KEY, snapshot['observation_ref'], None, binding)
        attempt, route = _attempt(bridge, 'verified')
        now = time.time()
        # Recreate an OAuth callback that reached verification before the
        # reset fence was installed; activation must still inspect the fence.
        with bridge.store.connect() as db:
            db.execute('INSERT INTO auth_attempts('
                       'id,owner,account_id,engine,name,mode,browser,status,data,created,updated) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (attempt['id'], attempt['owner'], attempt['account_id'],
                        attempt['engine'], attempt['name'], attempt['mode'],
                        attempt['browser'], attempt['status'], json.dumps({}), now, now))
        observation = {
            'provider': 'codex', 'status': 'active', 'disabled': False,
            'unavailable': False, 'models': [{'id': 'gpt-fixture'}],
            'binding_fingerprint': 'f' * 64,
            'identity_fingerprint': binding['identity_fingerprint'],
        }

        with pytest.raises(BridgeError) as error:
            bind_proxy_account(bridge.store, attempt, route, observation)
        assert error.value.code == 'reset_pending'
        assert bridge.store.proxy_binding('fixture') == binding
        assert bridge.store.get_auth_attempt(attempt['id'], attempt['owner'])['status'] == 'verified'
        assert bridge.store.pending_reset_attempt('fixture')['idempotency_key'] == FIRST_KEY
        assert provider_calls(state, 'POST') == []
