"""Opt-in execution of existing Store contracts against a private PostgreSQL fixture.

Set AGENTBRIDGE_TEST_POSTGRES_SOCKET to the owned fixture's Unix socket directory
and load with ``-p fixtures.test_postgres_store_fixture``. No services are discovered.
"""

import os
from pathlib import Path
from uuid import uuid4

import pytest

from agentbridge.storage.configuration import PostgresConfiguration
from agentbridge.storage.selection import FILENAME
from agentbridge.store import Store


@pytest.fixture(autouse=True)
def postgres_store(monkeypatch, tmp_path):
    socket = os.environ.get('AGENTBRIDGE_TEST_POSTGRES_SOCKET')
    if not socket:
        yield
        return
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    socket = Path(socket)
    assert socket.is_absolute() and socket.is_dir()
    assert socket.parent.stat().st_uid == os.getuid()
    assert socket.parent.stat().st_mode & 0o077 == 0
    original, allocated = Store.__init__, []
    host = tmp_path / 'postgres-host'
    host.mkdir(mode=0o700)

    def initialize(self, root, **kwargs):
        root = Path(root)
        if (kwargs.get('backend') is None and not (root / FILENAME).exists()
                and not (root / 'bridge.sqlite3').exists()):
            identifier = 'ab_t_' + uuid4().hex
            with psycopg.connect(host=str(socket), user='postgres', dbname='postgres',
                                 autocommit=True) as admin:
                admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB '
                                      'NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 8')
                              .format(sql.Identifier(identifier)))
                admin.execute(sql.SQL('CREATE SCHEMA {} AUTHORIZATION {}').format(
                    sql.Identifier(identifier), sql.Identifier(identifier)))
            allocated.append(identifier)
            connection = host / identifier
            connection.write_text(make_conninfo(host=str(socket), dbname='postgres', user=identifier))
            connection.chmod(0o600)
            kwargs.update(backend='postgresql', postgres=PostgresConfiguration(identifier, connection))
        original(self, root, **kwargs)

    monkeypatch.setattr(Store, '__init__', initialize)
    yield
    with psycopg.connect(host=str(socket), user='postgres', dbname='postgres',
                         autocommit=True) as admin:
        for identifier in allocated:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(identifier)))
            admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(identifier)))
