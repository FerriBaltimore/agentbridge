"""Real PG data migration with fixture host authority; not a Fullbrain drain acceptance gate."""

import json
import os
import sqlite3
import subprocess
import sys
from uuid import uuid4

import pytest

from agentbridge import Bridge
from agentbridge.checkpoint import observe_store
from agentbridge.errors import BridgeError
from agentbridge.storage import migrate_postgres
from agentbridge.store import Store
from fixtures.test_checkpoint_fixture import execution, prepared, retry, scope

pytestmark = pytest.mark.skipif(not os.environ.get('AGENTBRIDGE_TEST_POSTGRES_SOCKET'),
                                reason='Owned PostgreSQL fixture is not selected.')


def empty_destination(tmp_path):
    import psycopg
    from psycopg import sql

    allocated = Store(tmp_path / 'allocated')
    configuration = allocated.storage.configuration
    with psycopg.connect(host=os.environ['AGENTBRIDGE_TEST_POSTGRES_SOCKET'], user='postgres',
                         dbname='postgres', autocommit=True) as connection:
        connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(configuration.schema)))
        connection.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION {}').format(
            sql.Identifier(configuration.schema), sql.Identifier(configuration.schema)))
    return configuration


def source_store(tmp_path):
    Store(tmp_path / 'state', backend='sqlite', owner_ref='fixture-owner', durable=True)
    bridge = prepared(tmp_path)
    home = execution(bridge, payload_bytes=1024)
    bridge.store.finish('turn-1', 'completed', process_verified=True)
    checkpoint = retry(bridge)
    with bridge.store.connect() as connection:
        connection.execute("INSERT INTO events(seq,run_id,session_id,kind,at,data) "
                           "VALUES (1000,'deleted','instance','retired',1,'{}')")
        connection.execute('DELETE FROM events WHERE seq=1000')
    return bridge, home, checkpoint


def test_migration_keeps_native_coverage_ids_and_replay_and_fences_old_clients(tmp_path):
    bridge, home, checkpoint = source_store(tmp_path)
    destination = empty_destination(tmp_path)
    before = bridge.checkpoints.observe_store(format_version='1', params=scope(bridge))
    native = (home / 'state_5.sqlite').read_bytes()
    operation = str(uuid4())
    result = migrate_postgres(bridge.root, operation_id=operation,
                              owner_ref='fixture-owner', postgres=destination,
                              verify_quiescence=lambda proof: proof['owner_ref'] == 'fixture-owner')
    assert result['state'] == 'migrated'
    assert result['cursor'] == before['cursor']
    after = observe_store(bridge.root, format_version='1', params=scope_from(before))
    assert after['coverage'] == before['coverage']
    assert after['coverage'][0]['checkpoint_id'] == checkpoint['checkpoint_id']
    assert (home / 'state_5.sqlite').read_bytes() == native
    with pytest.raises(BridgeError) as stale:
        bridge.store.list('sessions')
    assert stale.value.code == 'store_authority_changed'
    with sqlite3.connect(bridge.root / 'bridge.sqlite3') as legacy:
        with pytest.raises(sqlite3.IntegrityError):
            legacy.execute("UPDATE sessions SET model='old-client'")
    reopened = Bridge(bridge.root)
    assert reopened.store.list('sessions')[0]['id'] == 'instance'
    reopened.store.emit('turn-1', 'after_migration', {})
    assert reopened.store.events(run_id='turn-1')[-1].seq > 1000
    assert migrate_postgres(bridge.root, operation_id=operation,
                            owner_ref='fixture-owner', postgres=destination,
                              verify_quiescence=lambda proof: proof['owner_ref'] == 'fixture-owner') == result
    with pytest.raises(BridgeError):
        Store(bridge.root, backend='sqlite')


def scope_from(value):
    return {key: value[key] for key in ('owner_ref', 'store_id', 'store_generation')}


def test_commit_before_local_ack_resumes_same_receipt_without_recopy(tmp_path, monkeypatch):
    from agentbridge.storage import migration

    bridge, _, _ = source_store(tmp_path)
    destination = empty_destination(tmp_path)
    operation = str(uuid4())
    save = migration.save

    def crash_before_activation(root, value, **kwargs):
        if value.get('backend') == 'postgresql' and 'migration_operation' not in value:
            raise RuntimeError('synthetic host crash after PostgreSQL COMMIT')
        return save(root, value, **kwargs)

    monkeypatch.setattr(migration, 'save', crash_before_activation)
    with pytest.raises(RuntimeError):
        migrate_postgres(bridge.root, operation_id=operation,
                         owner_ref='fixture-owner', postgres=destination,
                              verify_quiescence=lambda proof: proof['owner_ref'] == 'fixture-owner')
    with pytest.raises(BridgeError) as held:
        Bridge(bridge.root)
    assert held.value.code == 'store_migration_pending'
    monkeypatch.setattr(migration, 'save', save)
    result = migrate_postgres(bridge.root, operation_id=operation,
                              owner_ref='fixture-owner', postgres=destination,
                              verify_quiescence=lambda proof: proof['owner_ref'] == 'fixture-owner')
    assert result['state'] == 'migrated'
    assert 'migration_operation' not in json.loads((bridge.root / 'store-backend.json').read_text())


def test_legacy_process_with_open_sqlite_blocks_before_fencing_or_pg_writes(tmp_path):
    bridge, _, _ = source_store(tmp_path)
    destination = empty_destination(tmp_path)
    code = ('import sqlite3,sys,time; c=sqlite3.connect(sys.argv[1]); '
            'c.execute("SELECT version FROM metadata").fetchall(); '
            'print("ready",flush=True); time.sleep(15)')
    child = subprocess.Popen([sys.executable, '-c', code, str(bridge.root / 'bridge.sqlite3')],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        from agentbridge.process import alive, identity

        recorded = identity(child.pid)
        with pytest.raises(BridgeError) as held:
            migrate_postgres(bridge.root, operation_id=str(uuid4()),
                             owner_ref='fixture-owner', postgres=destination,
                              verify_quiescence=lambda proof: not alive(child.pid, recorded))
        assert held.value.code == 'store_migration_busy'
        assert json.loads((bridge.root / 'store-backend.json').read_text())['backend'] == 'sqlite'
        assert not (bridge.root / '.store-migration.json').exists()
    finally:
        child.terminate()
        child.wait(timeout=3)


def test_sql_failure_keeps_pending_and_rejects_a_different_destination(tmp_path, monkeypatch):
    import psycopg

    bridge, _, _ = source_store(tmp_path)
    destination = empty_destination(tmp_path)
    operation = str(uuid4())
    connect = psycopg.connect
    monkeypatch.setattr(psycopg, 'connect', lambda **_: (_ for _ in ()).throw(
        psycopg.OperationalError('private failure body')))
    with pytest.raises(BridgeError) as failed:
        migrate_postgres(bridge.root, operation_id=operation,
                         owner_ref='fixture-owner', postgres=destination,
                              verify_quiescence=lambda proof: proof['owner_ref'] == 'fixture-owner')
    assert failed.value.code == 'postgres_store_unavailable'
    assert 'private failure' not in str(failed.value)
    with pytest.raises(BridgeError) as pending:
        Store(bridge.root, backend='sqlite')
    assert pending.value.code == 'store_migration_pending'
    monkeypatch.setattr(psycopg, 'connect', connect)
    with pytest.raises(BridgeError) as changed:
        migrate_postgres(bridge.root, operation_id=str(uuid4()),
                         owner_ref='fixture-owner', postgres=destination,
                              verify_quiescence=lambda proof: proof['owner_ref'] == 'fixture-owner')
    assert changed.value.code == 'store_migration_conflict'
    assert migrate_postgres(bridge.root, operation_id=operation,
                            owner_ref='fixture-owner', postgres=destination,
                              verify_quiescence=lambda proof: proof['owner_ref'] == 'fixture-owner')['state'] == 'migrated'
