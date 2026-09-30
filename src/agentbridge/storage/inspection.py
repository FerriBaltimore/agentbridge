"""Read selected authority without creating a database, migrating or changing selection."""

from contextlib import contextmanager, closing
from pathlib import Path
import sqlite3

from ..errors import BridgeError
from .configuration import PostgresConfiguration
from .selection import load


@contextmanager
def connect_selected(root):
    root = Path(root)
    selected = load(root)
    if selected and selected['backend'] == 'postgresql':
        from .postgres import PostgresBackend

        configuration = PostgresConfiguration(selected['schema'], selected['conninfo_file'])
        with PostgresBackend(configuration).connect() as connection:
            connection.connection.execute('SET TRANSACTION READ ONLY')
            yield connection
        return
    path = root / 'bridge.sqlite3'
    if not path.exists():
        yield None
        return
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        yield connection


def inspect_postgres(root):
    selected = load(root)
    if not selected or selected['backend'] != 'postgresql':
        return None
    with connect_selected(root) as connection:
        rows = connection.execute('SELECT version FROM metadata LIMIT 2').fetchall()
        if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] != 15:
            raise BridgeError('runtime_incompatible', 'The Store schema is not supported.')
        row = connection.execute('SELECT enabled FROM store_identity WHERE singleton=1').fetchone()
        if row is None or row[0] != 1:
            raise BridgeError('native_identity_unknown', 'Durable Store identity is not enabled.')
    return {'state': 'present'}
