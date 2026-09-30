"""PostgreSQL-specific authority and failure controls; explicitly opt-in private fixture."""

from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from agentbridge import Bridge, PostgresConfiguration
from agentbridge.errors import BridgeError
from agentbridge.security import base_environment
from agentbridge.storage.postgres import PostgresBackend
from agentbridge.store import Store

pytestmark = pytest.mark.skipif(not os.environ.get('AGENTBRIDGE_TEST_POSTGRES_SOCKET'),
                                reason='Owned PostgreSQL Unix socket fixture is not selected.')


def prepared(tmp_path):
    store = Store(tmp_path / 'state', owner_ref='fixture-owner', durable=True)
    assert store.storage.name == 'postgresql', 'Load the explicit PostgreSQL fixture plugin.'
    return store


def test_lost_connection_rolls_back_and_release_preserves_replay(tmp_path):
    store = prepared(tmp_path)
    with pytest.raises(BridgeError):
        with store.connect() as connection:
            connection.execute('INSERT INTO events(run_id,session_id,kind,at,data) VALUES (?,?,?,?,?)',
                               ('turn', 'instance', 'lost', 1., '{}'))
            connection.connection.close()
    with store.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0
        connection.execute('INSERT INTO events(run_id,session_id,kind,at,data) VALUES (?,?,?,?,?)',
                           ('turn', 'instance', 'after', 2., '{}'))
    assert [event.kind for event in store.events(run_id='turn')] == ['after']


def test_roles_cannot_cross_store_scope_or_use_an_administrator(tmp_path):
    store = prepared(tmp_path)
    other = Store(tmp_path / 'other')
    with pytest.raises(BridgeError) as denied:
        with store.connect() as connection:
            connection.execute(f'SELECT * FROM {other.storage.configuration.schema}.accounts')
    assert denied.value.details == {'sqlstate': '42501'}
    secret_file = tmp_path / 'admin.private'
    import psycopg
    from psycopg.conninfo import make_conninfo

    secret_file.write_text(make_conninfo(host=os.environ['AGENTBRIDGE_TEST_POSTGRES_SOCKET'],
                                        dbname='postgres', user='postgres'))
    secret_file.chmod(0o600)
    configuration = replace(store.storage.configuration, conninfo_file=secret_file)
    with pytest.raises(BridgeError) as administrative:
        with PostgresBackend(configuration).connect():
            pass
    assert administrative.value.code == 'postgres_scope_denied'
    with psycopg.connect(**other.storage.configuration.connection_parameters()) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute('CREATE ROLE forbidden_role')


def test_selection_survives_unavailable_database_without_sqlite_fallback(tmp_path, monkeypatch):
    import psycopg

    store = prepared(tmp_path)
    configuration = store.storage.configuration
    original = psycopg.connect
    monkeypatch.setattr(psycopg, 'connect', lambda **_: (_ for _ in ()).throw(
        psycopg.OperationalError('synthetic-password-must-not-escape')))
    for backend in (None, 'postgresql'):
        with pytest.raises(BridgeError) as unavailable:
            Store(store.root, backend=backend)
        assert unavailable.value.code == 'postgres_store_unavailable'
        assert 'synthetic-password' not in str(unavailable.value)
        assert not (store.root / 'bridge.sqlite3').exists()
    # Initial selection is also published before a failed first connection.
    fresh = tmp_path / 'never-connected'
    with pytest.raises(BridgeError):
        Store(fresh, backend='postgresql', postgres=configuration)
    assert json.loads((fresh / 'store-backend.json').read_text())['backend'] == 'postgresql'
    monkeypatch.setattr(psycopg, 'connect', original)
    assert Store(fresh).storage.name == 'postgresql'
    with pytest.raises(BridgeError) as switch:
        Store(store.root, backend='sqlite')
    assert switch.value.code == 'store_migration_required'


def test_private_configuration_and_no_ambient_provider_inheritance(tmp_path, monkeypatch):
    store = prepared(tmp_path)
    configuration = store.storage.configuration
    monkeypatch.setenv('PGPASSWORD', 'synthetic-database-secret')
    assert 'PGPASSWORD' not in base_environment()
    with pytest.raises(BridgeError) as ambient:
        configuration.connection_parameters()
    assert ambient.value.code == 'unsafe_store_configuration'
    monkeypatch.delenv('PGPASSWORD')
    configuration.conninfo_file.chmod(0o644)
    with pytest.raises(BridgeError) as public:
        Store(store.root)
    assert public.value.code == 'unsafe_store_configuration'
    configuration.conninfo_file.chmod(0o600)
    selector = json.loads((store.root / 'store-backend.json').read_text())
    assert set(selector) == {'format_version', 'backend', 'schema', 'conninfo_file'}
    with pytest.raises(BridgeError):
        PostgresConfiguration('public', configuration.conninfo_file)


def test_complete_inventory_and_capability_do_not_claim_sql_backup(tmp_path):
    store = prepared(tmp_path)
    with store.connect() as connection:
        names = {row[0] for row in connection.execute(
            'SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()')}
        assert {'error_diagnoses', 'error_cases', 'permission_requests', 'provider_inspections',
                'credential_capture_operations', 'credential_reconnections'} <= names
        assert connection.execute('SHOW synchronous_commit').fetchone()[0] == 'on'
    bridge = Bridge(store.root)
    capability = bridge.capabilities()['operations']['checkpoints.snapshot_store']
    assert capability['support'] == 'unsupported'
    assert capability['limitations'] == ['checkpoint_sql_backup_required']
    assert not (store.root / 'bridge.sqlite3').exists()
