"""Real private supervisor/socket plus synthetic writer protocol, without provider inference."""

import http.client
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import pytest

from agentbridge import Account, Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.bundle import resolve_grantbridge_adapter
from agentbridge.proxy import credential_content as capsule
from agentbridge.proxy.credential_barrier import hold
from agentbridge.proxy.managed import ManagedProxyClient
from agentbridge.proxy.process_lifecycle import _record_process_running
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from test_credential_snapshots import target
from test_managed_proxy import managed

__all__ = ['managed']


@pytest.fixture
def online(tmp_path, monkeypatch):
    executable = tmp_path / 'writer'
    fixture = Path(__file__).parent / 'fixtures' / 'test_credential_writer_fixture.py'
    executable.write_text(f'#!{sys.executable}\nimport runpy\n'
                          f'runpy.run_path({str(fixture)!r}, run_name="__main__")\n')
    executable.chmod(0o700)
    monkeypatch.setenv('AGENTBRIDGE_CLIPROXY_BIN', str(executable))
    monkeypatch.setenv('HOME', str(tmp_path))
    bridge = Bridge(tmp_path / 'state', owner_ref='fixture-owner', durable=True)
    try:
        yield bridge
    finally:
        bridge.managed_proxy.shutdown()
        bridge.close()


def account(bridge, account_id):
    route = bridge.managed_proxy.provision(account_id)
    seed_authenticated_proxy_account(bridge.store, Account(
        account_id, 'codex', provider='codex', supported_models=('gpt-5',), **route))
    with bridge.store.connect() as db:
        db.execute('UPDATE auth_proxy_routes SET connection=? WHERE attempt_id=?',
                   (json.dumps({'adapter': str(resolve_grantbridge_adapter(bridge.root)),
                                'data_dir': None}), 'fixture-login-' + account_id))
    rotate(route, 'synthetic-' + account_id)
    return route


def rotate(route, value):
    port = int(route['proxy_base_url'].split(':')[2].split('/')[0])
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        connection.request('POST', '/fixture/rotate', value.encode(),
                           {'Authorization': 'Bearer ' + os.environ[route['management_key_env']]})
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read())['persisted']
    finally:
        connection.close()


def capture(bridge, *accounts, operation_id=None):
    return bridge.credential_snapshots.capture_online(operation_id=operation_id or str(uuid4()),
                                                      account_ids=accounts)


def test_online_capture_preserves_active_account_and_observes_without_sql_writes(online, tmp_path):
    first, other = account(online, 'first'), account(online, 'other')
    records = [json.loads((online.root / 'managed-proxies/accounts' / name / 'route.json').read_text())
               for name in ('first', 'other')]
    online.store.add_session('active-instance', 'first', str(tmp_path), 'gpt-5')
    online.store.admit('active-turn', 'active-instance', 'synthetic only', RunOptions(), 'active')
    descriptor = capture(online, 'first', 'other')
    configuration = online.credential_snapshots.configuration()
    assert configuration['cliproxyapi'] == {'configured': True, 'account_ids': ['first', 'other']}
    assert configuration['grantbridge_login'] == {'configured': False}
    assert 'synthetic-' not in json.dumps(descriptor)
    assert 'proxy_base_url' not in json.dumps(descriptor)
    with online.store.connect() as db:
        version = db.execute('PRAGMA data_version').fetchone()[0]
        for _ in range(3):
            assert online.credential_snapshots.verify_current(
                snapshot_id=descriptor['snapshot_id'])['current']
        assert db.execute('PRAGMA data_version').fetchone()[0] == version
        assert not hold(db, 'first') and not hold(db, 'other')
        assert db.execute('SELECT state FROM runs WHERE id=?', ('active-turn',)).fetchone()[0] == 'starting'
    assert all(_record_process_running(record) for record in records)


    assert online.managed_proxy.ensure('first', first['proxy_base_url']) == first
    rotate(other, 'synthetic-rotated')
    with pytest.raises(BridgeError) as stale:
        online.credential_snapshots.verify_current(snapshot_id=descriptor['snapshot_id'])
    assert stale.value.code == 'credential_snapshot_pending'
    assert capture(online, 'first', 'other', operation_id=descriptor['snapshot_id']) == descriptor
    new = capture(online, 'first', 'other')
    assert online.credential_snapshots.verify_current(snapshot_id=new['snapshot_id'])['current']
    assert all(_record_process_running(record) for record in records)


def test_configuration_does_not_label_unknown_legacy_storage_absent(online):
    assert online.credential_snapshots.configuration()['cliproxyapi']['configured'] is False
    account(online, 'first')
    with online.store.connect() as db:
        db.execute('UPDATE auth_proxy_routes SET connection=?',
                   (json.dumps({'adapter': '/unknown/agentbridge-adapter.mjs'}),))
    with pytest.raises(BridgeError) as unknown:
        online.credential_snapshots.configuration()
    assert unknown.value.code == 'credential_snapshot_unsupported'


def test_retired_credentials_are_not_recoverable_and_incomplete_retirement_is_pending(online):
    account(online, 'first')
    account(online, 'other')
    before = capture(online, 'first', 'other')
    online.store.retire_account('other')
    with pytest.raises(BridgeError) as incomplete:
        online.credential_snapshots.configuration()
    assert incomplete.value.code == 'credential_snapshot_pending'
    online.account_delete(account_id='other')
    with online.store.connect() as db:
        # A terminal legacy route is not an active credential layout to migrate or recover.
        db.execute('UPDATE accounts SET config=? WHERE id=?',
                   (json.dumps({'id': 'other', 'proxy_base_url': 'http://127.0.0.1:1'}), 'other'))
        db.execute('INSERT INTO auth_proxy_routes(attempt_id,config,connection) VALUES (?,?,?)',
                   ('fixture-login-other', '{}', json.dumps({'adapter': '/old/custom-provider'})))
    configured = online.credential_snapshots.configuration()['cliproxyapi']
    assert configured == {'configured': True, 'account_ids': ['first']}
    with pytest.raises(BridgeError) as stale:
        online.credential_snapshots.verify_current(snapshot_id=before['snapshot_id'],
                                                   account_ids=configured['account_ids'])
    assert stale.value.code == 'credential_snapshot_pending'
    descriptor = capture(online, *configured['account_ids'])
    assert descriptor['credential_refs'] == ['first']
    assert online.store.retirement_status('other')['retired']
    assert online.credential_snapshots.verify_current(snapshot_id=descriptor['snapshot_id'])['current']


def test_rotation_during_collection_retries_fresh_and_preserves_original_operation(online, monkeypatch):
    first = account(online, 'first')
    account(online, 'other')
    original = online.managed_proxy.snapshot_files
    def changing(account_id, capture_id):
        reply = original(account_id, capture_id)
        if account_id == 'other':
            rotate(first, 'synthetic-rotation-during-capture')
        return reply
    monkeypatch.setattr(online.managed_proxy, 'snapshot_files', changing)
    operation = str(uuid4())
    with pytest.raises(BridgeError) as pending:
        capture(online, 'first', 'other', operation_id=operation)
    assert pending.value.code == 'credential_snapshot_pending'
    monkeypatch.setattr(online.managed_proxy, 'snapshot_files', original)
    descriptor = capture(online, 'first', 'other', operation_id=operation)
    assert online.credential_snapshots.verify_current(snapshot_id=descriptor['snapshot_id'])['current']


def test_lost_ack_reuses_seal_and_restore_drops_freshness_authority(online, tmp_path, monkeypatch):
    account(online, 'first')
    original = capsule.publish_files
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('private-synthetic-error')
    monkeypatch.setattr(capsule, 'publish_files', lost)
    operation = str(uuid4())
    with pytest.raises(BridgeError) as failure:
        capture(online, 'first', operation_id=operation)
    assert 'private-synthetic' not in str(failure.value)
    monkeypatch.setattr(capsule, 'publish_files', original)
    descriptor = capture(online, 'first', operation_id=operation)
    restored = target(online, tmp_path, descriptor)
    try:
        assert restored.credential_snapshots.restore(descriptor)['held']
        with pytest.raises(BridgeError):
            restored.credential_snapshots.verify_current(snapshot_id=operation)
        materialized = restored.root / 'managed-proxies/accounts/first/auth/credential.json'
        assert materialized.read_text() == 'synthetic-first'
        assert materialized.stat().st_mode & 0o777 == 0o600
    finally:
        restored.close()


def test_old_writer_refuses_online_capture_without_stopping_it(managed):
    _, root = managed
    with Bridge(root, owner_ref='fixture-owner', durable=True) as bridge:
        route = bridge.managed_proxy.provision('first')
        seed_authenticated_proxy_account(bridge.store, Account(
            'first', 'codex', provider='codex', supported_models=('gpt-5',), **route))
        with pytest.raises(BridgeError) as unsupported:
            capture(bridge, 'first')
        assert unsupported.value.code == 'credential_snapshot_unsupported'
        assert bridge.managed_proxy.ensure('first', route['proxy_base_url']) == route


def test_administrative_client_never_starts_a_fallback_supervisor(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Administrative snapshot client must not launch a process')
    monkeypatch.setattr('agentbridge.proxy.managed.subprocess.Popen', forbidden)
    client = ManagedProxyClient(tmp_path / 'absent', allow_start=False)
    with pytest.raises(BridgeError) as pending:
        client.credential_revision('fixture')
    assert pending.value.code == 'credential_snapshot_pending'
