"""Account-scoped credential capture against real isolated fixture proxy processes."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from agentbridge import Account, Bridge, RunOptions
from agentbridge.checkpoint import state
from agentbridge.checkpoint.snapshot import restore_store
from agentbridge.errors import BridgeError
from agentbridge.proxy.credential_barrier import hold
from agentbridge.proxy.process_lifecycle import _record_process_running
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from test_managed_proxy import managed


def account(bridge, account_id):
    route = bridge.managed_proxy.provision(account_id)
    home = bridge.root / 'managed-proxies/accounts' / account_id
    observed = {'name': 'fixture.json', 'auth_index': 'fixture-path', 'account_type': 'oauth',
                'provider': 'codex', 'status': 'active', 'disabled': False, 'unavailable': False,
                'source': 'file', 'runtime_only': False, 'cooldowns': [],
                'id_token': {'chatgpt_account_id': 'identity-' + account_id}}
    (home / 'auth/fixture-observation.json').write_text(json.dumps(observed))
    (home / 'auth/private.json').write_text('private-synthetic-token')
    seed_authenticated_proxy_account(bridge.store, Account(
        account_id, 'codex', provider='codex', supported_models=('gpt-5',), **route),
        observe_local=True)
    return home, route


def capture(bridge, account_ids, *, operation_id=None, proof_ref=None, verifier=None):
    return bridge.credential_snapshots.capture(operation_id=operation_id or str(uuid4()),
        account_ids=account_ids, proof_ref=proof_ref or str(uuid4()),
        verify_quiescence=verifier or (lambda scope: True))


def target(source, tmp_path, descriptor):
    identity = source.checkpoints.identity()
    params = {key: identity[key] for key in state.IDENTITY_KEYS}
    snapshot = source.checkpoints.snapshot_store(format_version='1', operation_id=str(uuid4()),
                                                params=params)
    restore_store(tmp_path / 'restored', snapshot,
                  source.checkpoints.resolve_content(snapshot['content']), owner_ref='fixture-owner')
    bridge = Bridge(tmp_path / 'restored')
    bridge.credential_snapshots.register_content(descriptor['content'],
        source.credential_snapshots.resolve_content(descriptor['content']))
    return bridge


def test_capture_stops_only_selected_account_and_preserves_route_and_queue(managed, tmp_path):
    _, root = managed
    bridge = Bridge(root, owner_ref='fixture-owner', durable=True)
    first, route = account(bridge, 'first')
    other, other_route = account(bridge, 'other')
    first_record = json.loads((first / 'route.json').read_text())
    other_record = json.loads((other / 'route.json').read_text())
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    bridge.store.add_session('instance', 'first', str(workspace), 'gpt-5')
    queued = bridge.queue_add('instance', 'Queued until the credential capture completes.')
    operation_id, proof_ref = str(uuid4()), str(uuid4())
    scopes = []

    def proof(scope):
        scopes.append(scope)
        assert not _record_process_running(first_record)
        assert _record_process_running(other_record)
        with bridge.store.connect() as db:
            assert state.blocked(db, 'instance')
            with pytest.raises(BridgeError, match='held'):
                state.require_admission(db, 'instance')
        with pytest.raises(BridgeError) as error:
            bridge.managed_proxy.ensure('first', route['proxy_base_url'])
        assert error.value.code == 'credential_snapshot_pending'
        with pytest.raises(BridgeError) as error:
            bridge.account_login_start(provider='codex', name='first')
        assert error.value.code == 'credential_snapshot_pending'
        return True

    descriptor = capture(bridge, ['first'], operation_id=operation_id, proof_ref=proof_ref,
                         verifier=proof)
    assert descriptor['credential_refs'] == ['first']
    assert scopes[0]['owner_ref'] == 'fixture-owner'
    assert descriptor['restore_authentication'] == 'unverified'
    path = bridge.credential_snapshots.resolve_content(descriptor['content'])
    data = path.read_bytes()
    assert b'private-synthetic-token' in data
    assert b'route.json' not in data and os.environ[route['key_env']].encode() not in data
    assert bridge.managed_proxy.ensure('other', other_route['proxy_base_url']) == other_route
    assert json.loads((other / 'route.json').read_text())['pid'] == other_record['pid']
    assert capture(bridge, ['first'], operation_id=operation_id, proof_ref=proof_ref,
                   verifier=lambda scope: pytest.fail('repeat capture is already sealed')) == descriptor
    assert bridge.managed_proxy.ensure('first', route['proxy_base_url']) == route
    assert json.loads((first / 'route.json').read_text())['pid'] != first_record['pid']
    assert bridge.queue_list('instance')['items'][0]['message_id'] == queued['message_id']


def test_failed_proof_survives_restart_and_does_not_expose_callback_error(managed):
    _, root = managed
    bridge = Bridge(root, owner_ref='fixture-owner', durable=True)
    _, route = account(bridge, 'first')
    operation_id, proof_ref = str(uuid4()), str(uuid4())
    def rejected(scope):
        raise RuntimeError('private-synthetic-token')
    with pytest.raises(BridgeError) as error:
        capture(bridge, ['first'], operation_id=operation_id, proof_ref=proof_ref, verifier=rejected)
    assert 'private-synthetic-token' not in str(error.value)
    restarted = Bridge(root)
    with restarted.store.connect() as db:
        assert hold(db, 'first')[0] == operation_id
    with pytest.raises(BridgeError):
        restarted.managed_proxy.ensure('first', route['proxy_base_url'])
    descriptor = capture(restarted, ['first'], operation_id=operation_id, proof_ref=proof_ref)
    assert descriptor['snapshot_id'] == operation_id


def test_restore_is_held_without_old_runtime_authority_and_rejects_unverified(managed, tmp_path):
    _, root = managed
    bridge = Bridge(root, owner_ref='fixture-owner', durable=True)
    original, _ = account(bridge, 'first')
    descriptor = capture(bridge, ['first'])
    restored = target(bridge, tmp_path, descriptor)
    try:
        assert restored.credential_snapshots.restore(descriptor)['held']
        assert restored.credential_snapshots.restore(descriptor)['held']
        home = restored.root / 'managed-proxies/accounts/first'
        assert not (home / 'route.json').exists()
        assert (home / 'auth/private.json').read_text() == 'private-synthetic-token'
        assert (home / 'auth/private.json').stat().st_mode & 0o777 == 0o600
        with pytest.raises(BridgeError):
            restored.managed_proxy.provision('first')
        with pytest.raises(BridgeError) as error:
            restored.credential_snapshots.verify_restored('first', proof_ref=str(uuid4()),
                verify_authority=lambda scope: False,
                verify_credential=lambda *args: pytest.fail('no provider before authority'))
        assert error.value.code == 'credential_snapshot_authority'
        assert not (home / 'route.json').exists()
        with pytest.raises(BridgeError) as error:
            restored.credential_snapshots.verify_restored('first', proof_ref=str(uuid4()),
                verify_authority=lambda scope: True,
                verify_credential=lambda *args: {'authentication': 'revoked'})
        assert error.value.code == 'credential_snapshot_reauthentication'
        with restored.store.connect() as db:
            assert hold(db, 'first')
        assert not _record_process_running(json.loads((home / 'route.json').read_text()))
        proof_ref = str(uuid4())
        result = restored.credential_snapshots.verify_restored('first', proof_ref=proof_ref,
            verify_authority=lambda scope: True,
            verify_credential=lambda scope, route, observation: {
                'authentication': 'verified',
                'identity_fingerprint': observation['identity_fingerprint']})
        assert result == {'account_id': 'first', 'authentication': 'verified', 'held': False}
        assert restored.credential_snapshots.verify_restored('first', proof_ref=proof_ref,
            verify_authority=lambda scope: pytest.fail('already accepted'),
            verify_credential=lambda *args: pytest.fail('already accepted')) == result
        with restored.store.connect() as db:
            assert not hold(db, 'first')
            assert state.identity(db)['recovery_held']  # native Store release is independent
        assert (original / 'auth/private.json').exists()
        second = capture(restored, ['first'])
        again = target(restored, tmp_path / 'second', second)
        try:
            with again.store.connect() as db:
                assert hold(db, 'first')[1] == 'restore_pending'
                assert not db.execute('SELECT 1 FROM credential_restored_accounts').fetchone()
            assert again.credential_snapshots.restore(second)['held']
            with pytest.raises(BridgeError) as error:
                again.credential_snapshots.verify_restored('first', proof_ref=proof_ref,
                    verify_authority=lambda scope: False,
                    verify_credential=lambda *args: pytest.fail('old proof cannot release'))
            assert error.value.code == 'credential_snapshot_authority'
        finally:
            again.managed_proxy.shutdown()
    finally:
        restored.managed_proxy.shutdown()


def test_capture_rejects_links_and_holds_until_private_repair(managed, tmp_path):
    _, root = managed
    bridge = Bridge(root, owner_ref='fixture-owner', durable=True)
    home, _ = account(bridge, 'first')
    (home / 'auth/unsafe').symlink_to(tmp_path)
    operation_id, proof_ref = str(uuid4()), str(uuid4())
    with pytest.raises(BridgeError) as error:
        capture(bridge, ['first'], operation_id=operation_id, proof_ref=proof_ref)
    assert error.value.code == 'credential_snapshot_corrupt'
    with bridge.store.connect() as db:
        assert hold(db, 'first')
    (home / 'auth/unsafe').unlink()
    assert capture(bridge, ['first'], operation_id=operation_id, proof_ref=proof_ref)


def test_verified_restore_rebinds_login_route_for_existing_session(managed, tmp_path):
    _, root = managed
    source = Bridge(root, owner_ref='fixture-owner', durable=True)
    _, old_route = account(source, 'first')
    account(source, 'other')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    source.store.add_session('existing-instance', 'first', str(workspace), 'gpt-5')
    descriptor = capture(source, ['first'])
    restored = target(source, tmp_path, descriptor)
    try:
        restored.credential_snapshots.restore(descriptor)
        with restored.store.connect() as db:
            provenance = dict(db.execute(
                "SELECT * FROM auth_attempts WHERE account_id='first'").fetchone())
            other_route = db.execute("SELECT config FROM auth_proxy_routes "
                                     "WHERE attempt_id='fixture-login-other'").fetchone()[0]
        restored.credential_snapshots.verify_restored('first', proof_ref=str(uuid4()),
            verify_authority=lambda scope: True,
            verify_credential=lambda scope, route, observation: {
                'authentication': 'verified',
                'identity_fingerprint': observation['identity_fingerprint']})
        current = restored.account('first')
        assert current.proxy_base_url != old_route['proxy_base_url']
        assert restored.store.proxy_login_origin(current)
        assert restored.routes.observation(current, refresh=True,
            include_catalog=False)['data']['binding_verified']
        with restored.store.connect() as db:
            assert state.identity(db)['recovery_held']
            assert dict(db.execute("SELECT * FROM auth_attempts WHERE account_id='first'"
                                   ).fetchone()) == provenance
            assert db.execute("SELECT config FROM auth_proxy_routes "
                              "WHERE attempt_id='fixture-login-other'").fetchone()[0] == other_route
        identity = restored.checkpoints.identity()
        restored.checkpoints.release_recovery(expected_generation=identity['store_generation'],
                                             ready_instances=['existing-instance'])
        restored.store.admit('after-restore', 'existing-instance', 'admitted only',
                             RunOptions(), 'after')
        restored.store.finish('after-restore', 'failed', process_verified=True)
        assert restored.instance_get('existing-instance')['account_id'] == 'first'
        assert source.account('first').proxy_base_url == old_route['proxy_base_url']
    finally:
        restored.managed_proxy.shutdown()


def test_capture_drains_execution_and_login_before_stopping_the_proxy(managed, tmp_path):
    _, root = managed
    bridge = Bridge(root, owner_ref='fixture-owner', durable=True)
    home, _ = account(bridge, 'first')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    bridge.store.add_session('instance', 'first', str(workspace), 'gpt-5')
    bridge.store.admit('turn', 'instance', 'synthetic prompt', RunOptions(), 'request')
    operation_id, proof_ref = str(uuid4()), str(uuid4())
    record = json.loads((home / 'route.json').read_text())
    with pytest.raises(BridgeError) as error:
        capture(bridge, ['first'], operation_id=operation_id, proof_ref=proof_ref,
                verifier=lambda scope: pytest.fail('active execution cannot be proven drained'))
    assert error.value.code == 'credential_snapshot_pending'
    assert _record_process_running(record)
    bridge.store.finish('turn', 'failed', process_verified=True)
    with bridge.store.connect() as db:
        db.execute("UPDATE auth_attempts SET status='awaiting_user' WHERE id='fixture-login-first'")
    with pytest.raises(BridgeError) as error:
        capture(bridge, ['first'], operation_id=operation_id, proof_ref=proof_ref,
                verifier=lambda scope: pytest.fail('unfinished login cannot be proven drained'))
    assert error.value.code == 'credential_snapshot_pending'
    assert _record_process_running(record)
    with bridge.store.connect() as db:
        db.execute("UPDATE auth_attempts SET status='bound' WHERE id='fixture-login-first'")
    assert capture(bridge, ['first'], operation_id=operation_id, proof_ref=proof_ref)
