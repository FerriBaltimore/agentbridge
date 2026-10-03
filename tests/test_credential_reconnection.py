"""Restored credential reconnection uses real SDK, bundled RPC and fixture OAuth callbacks."""

import os
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.proxy.credential_barrier import hold
from test_credential_snapshots import account, capture, target
from test_managed_proxy import managed


@pytest.fixture(autouse=True)
def synthetic_callback_port(monkeypatch):
    # The fixture delivers callbacks by RPC and never binds the provider's host port.
    monkeypatch.setattr('agentbridge.grantbridge.ensure_callback_port_available',
                        lambda provider: None)


def callback(bridge, started, code='fixture-good', owner=None):
    state = parse_qs(urlsplit(started['authorization_url']).query)['state'][0]
    return bridge.account_login_callback(started['attempt_id'],
        owner_ref=owner or started['owner_ref'],
        redirect_url=f'http://localhost:1455/auth/callback?state={state}&code={code}')


def prepare(managed, tmp_path):
    _, root = managed
    source = Bridge(root, owner_ref='fixture-owner', durable=True)
    account(source, 'first')
    account(source, 'other')
    descriptor = capture(source, ['first'])
    restored = target(source, tmp_path, descriptor)
    restored.credential_snapshots.restore(descriptor)
    return source, restored


def test_revoked_restore_reconnects_only_after_owned_same_identity_login(managed, tmp_path):
    source, restored = prepare(managed, tmp_path)
    try:
        with pytest.raises(BridgeError) as error:
            restored.credential_snapshots.verify_restored('first', proof_ref=str(uuid4()),
                verify_authority=lambda scope: True,
                verify_credential=lambda *args: {'authentication': 'revoked'})
        assert error.value.code == 'credential_snapshot_reauthentication'
        old = restored.account('first')
        old_key = os.environ[old.key_env]
        proof_ref = str(uuid4())
        scopes = []
        with pytest.raises(BridgeError) as error:
            restored.credential_snapshots.authorize_reconnection('first', proof_ref=proof_ref,
                verify_authority=lambda scope: False)
        assert error.value.code == 'credential_snapshot_authority'
        result = restored.credential_snapshots.authorize_reconnection('first', proof_ref=proof_ref,
            verify_authority=lambda scope: scopes.append(scope) or True)
        assert result['held'] and result['login_allowed']
        assert scopes[0]['account_ids'] == ['first']
        identity = restored.checkpoints.identity()
        assert scopes[0]['store_generation'] == identity['store_generation']
        current = restored.account('first')
        assert current.proxy_base_url != old.proxy_base_url
        assert os.environ[current.key_env] != old_key
        auth = restored.root / 'managed-proxies/accounts/first/auth'
        assert not list(auth.iterdir())
        quarantine = restored.root / 'managed-proxies/quarantine' / identity['store_generation']
        assert (quarantine / 'first' / proof_ref / 'auth/private.json').read_text() == (
            'private-synthetic-token')
        record = (auth.parent / 'route.json').read_bytes()
        restarted = Bridge(restored.root)
        assert restarted.credential_snapshots.authorize_reconnection('first', proof_ref=proof_ref,
            verify_authority=lambda scope: pytest.fail('accepted proof must not run twice')) == result
        assert (auth.parent / 'route.json').read_bytes() == record
        with pytest.raises(BridgeError) as error:
            restarted.credential_snapshots.authorize_reconnection('other', proof_ref=proof_ref,
                verify_authority=lambda scope: pytest.fail('proof is scoped to first'))
        assert error.value.code == 'credential_snapshot_conflict'
        with pytest.raises(BridgeError):
            restarted.managed_proxy.ensure('first', current.proxy_base_url)
        restarted.checkpoints.release_recovery(expected_generation=identity['store_generation'])
        workspace = tmp_path / 'workspace'
        workspace.mkdir()
        with pytest.raises(BridgeError) as error:
            restarted.store.add_session('instance', 'first', str(workspace), 'gpt-5')
        assert error.value.code == 'credential_snapshot_pending'
        started = restarted.account_login_start(provider='codex', name='first', request_key='relogin')
        assert started['status'] == 'awaiting_user'
        with pytest.raises(BridgeError) as error:
            callback(restarted, started, owner='outsider')
        assert error.value.code == 'authentication_attempt_not_found'
        assert not list(auth.iterdir())
        callback(restarted, started)
        checked = restarted.account_login_check(started['attempt_id'], owner_ref=started['owner_ref'])
        assert checked['status'] == 'verified'
        with restarted.store.connect() as db:
            assert hold(db, 'first')  # check alone cannot activate a route
        bound = restarted.account_login_complete(started['attempt_id'], owner_ref=started['owner_ref'])
        assert bound['account']['id'] == 'first'
        with restarted.store.connect() as db:
            assert not hold(db, 'first')
        assert restarted.credential_snapshots.authorize_reconnection('first', proof_ref=proof_ref,
            verify_authority=lambda scope: pytest.fail('already bound'))['authentication'] == 'verified'
        assert restarted.routes.observation(restarted.account('first'), refresh=True,
                                             include_catalog=False)['data']['binding_verified']
        restarted.store.add_session('instance', 'first', str(workspace), 'gpt-5')
        restarted.store.admit('after-login', 'instance', 'admitted only', RunOptions(), 'after')
        restarted.store.finish('after-login', 'failed', process_verified=True)
        assert source.store.proxy_binding('first')['identity_fingerprint'] == (
            restarted.store.proxy_binding('first')['identity_fingerprint'])
    finally:
        restored.managed_proxy.shutdown()


def test_identity_mismatch_and_interrupted_preparation_remain_held(managed, tmp_path, monkeypatch):
    _, restored = prepare(managed, tmp_path)
    try:
        proof_ref = str(uuid4())
        with monkeypatch.context() as patch:
            patch.setattr(restored.managed_proxy, 'reconnect_route',
                          lambda *args: (_ for _ in ()).throw(RuntimeError('private-token')))
            with pytest.raises(BridgeError) as error:
                restored.credential_snapshots.authorize_reconnection('first', proof_ref=proof_ref,
                    verify_authority=lambda scope: True)
        assert 'private-token' not in str(error.value)
        with restored.store.connect() as db:
            assert hold(db, 'first')[1] == 'reconnect_preparing'
        with pytest.raises(BridgeError):
            restored.account_login_start(provider='codex', name='first')
        restarted = Bridge(restored.root)
        assert restarted.credential_snapshots.authorize_reconnection('first', proof_ref=proof_ref,
            verify_authority=lambda scope: True)['login_allowed']
        wrong = restarted.account_login_start(provider='codex', name='first', request_key='wrong')
        callback(restarted, wrong, 'fixture-wrong')
        with pytest.raises(BridgeError) as error:
            restarted.account_login_check(wrong['attempt_id'], owner_ref=wrong['owner_ref'])
        assert error.value.code == 'identity_changed'
        with restarted.store.connect() as db:
            assert hold(db, 'first')[1] == 'reconnect'
        assert restarted.account_login_status(wrong['attempt_id'], owner_ref=wrong['owner_ref'])[
            'status'] == 'failed'
        again = Bridge(restored.root)
        with pytest.raises(BridgeError):
            again.managed_proxy.ensure('first', again.account('first').proxy_base_url)
        next_proof = str(uuid4())
        assert again.credential_snapshots.authorize_reconnection('first', proof_ref=next_proof,
            verify_authority=lambda scope: True)['held']
        with pytest.raises(BridgeError) as error:
            again.credential_snapshots.authorize_reconnection('first', proof_ref=proof_ref,
                verify_authority=lambda scope: pytest.fail('superseded proof cannot return'))
        assert error.value.code == 'credential_snapshot_conflict'
        good = again.account_login_start(provider='codex', name='first', request_key='good')
        callback(again, good)
        again.account_login_check(good['attempt_id'], owner_ref=good['owner_ref'])
        assert again.account_login_complete(good['attempt_id'], owner_ref=good['owner_ref'])[
            'account']['id'] == 'first'
        descriptor = capture(again, ['first'])
        pending = again.account_login_start(provider='codex', name='first',
                                             request_key='pending-at-sql-cut')
        second = target(again, tmp_path / 'second', descriptor)
        try:
            with second.store.connect() as db:
                assert hold(db, 'first')[1] == 'restore_pending'
                assert not db.execute('SELECT 1 FROM credential_reconnections').fetchone()
            interrupted = second.store.get_auth_attempt(pending['attempt_id'], pending['owner_ref'])
            assert interrupted['status'] == 'interrupted'
            assert interrupted['data']['recovery']['previous_status'] == 'awaiting_user'
            assert interrupted['data']['recovery']['outcome'] == 'unknown'
            assert second.account_login_cancel(pending['attempt_id'], owner_ref=pending['owner_ref'])[
                'status'] == 'abandoned'
            with pytest.raises(BridgeError) as error:
                second.credential_snapshots.authorize_reconnection('first', proof_ref=next_proof,
                    verify_authority=lambda scope: False)
            assert error.value.code == 'credential_snapshot_authority'
            assert second.credential_snapshots.authorize_reconnection('first', proof_ref=str(uuid4()),
                verify_authority=lambda scope: True)['login_allowed']
        finally:
            second.managed_proxy.shutdown()
    finally:
        restored.managed_proxy.shutdown()
