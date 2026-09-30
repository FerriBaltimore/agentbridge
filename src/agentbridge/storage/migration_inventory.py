"""Canonical, streaming row comparison; preserves legacy row ordering and sequence gaps."""

import hashlib
import json
import re

from ..errors import BridgeError
from .sql_dialect import ORDERED


def identifier(value):
    if not re.fullmatch(r'[a-z_][a-z0-9_]*', value):
        raise BridgeError('store_migration_incompatible', 'Unknown Store schema identifiers.')
    return '"' + value + '"'


def tables(source):
    return [row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                           "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def columns(source, table):
    return [(row[1], row[2]) for row in source.execute(f'PRAGMA table_info({identifier(table)})')]


def rows(connection, table, fields, *, postgres=False):
    selected = [identifier(name) for name, _ in fields]
    if table in ORDERED:
        selected += ['_ab_order' if postgres else 'rowid']
        order = ['_ab_order' if postgres else 'rowid']
    else:
        collation = '"C"' if postgres else 'BINARY'
        order = [identifier(name) + (f' COLLATE {collation}' if kind.upper() == 'TEXT' else '')
                 for name, kind in fields]
    query = f'SELECT {",".join(selected)} FROM {identifier(table)} ORDER BY {",".join(order)}'
    return connection.execute(query)


def inventory(connection, source, *, postgres=False):
    result = {}
    for table in tables(source):
        fields = columns(source, table)
        checksum, count = hashlib.sha256(), 0
        for row in rows(connection, table, fields, postgres=postgres):
            checksum.update(json.dumps(list(row), ensure_ascii=True, allow_nan=False,
                                       separators=(',', ':')).encode())
            checksum.update(b'\n')
            count += 1
        result[table] = {'rows': count, 'sha256': checksum.hexdigest()}
    return result


def copy_rows(source, destination):
    from psycopg import sql

    names = {row[0] for row in destination.execute('SELECT table_name FROM information_schema.tables '
                                                  'WHERE table_schema=current_schema()')}
    if not set(tables(source)) <= names:
        raise BridgeError('store_migration_incompatible', 'The source has unsupported Store tables.')
    for table in sorted(names - {'store_migration_receipt'}):
        destination.execute(f'DELETE FROM {identifier(table)}')
    for table in tables(source):
        fields = columns(source, table)
        expected = {name for name, _ in fields}
        actual = {row[1] for row in destination.execute(f'PRAGMA table_info({identifier(table)[1:-1]})')}
        if actual - {'_ab_order'} != expected:
            raise BridgeError('store_migration_incompatible', 'Store columns do not match.')
        names = [name for name, _ in fields] + (['_ab_order'] if table in ORDERED else [])
        query = (f'INSERT INTO {identifier(table)} ({",".join(map(identifier, names))}) '
                 f'VALUES ({",".join("?" for _ in names)})')
        batch = []
        for row in rows(source, table, fields):
            batch.append(tuple(row))
            if len(batch) >= 256:
                destination.executemany(query, batch)
                batch.clear()
        if batch:
            destination.executemany(query, batch)
    # Explicit IDs do not advance identity sequences. Preserve deleted AUTOINCREMENT gaps,
    # not just MAX(live rows); event replay must never recycle a formerly issued sequence.
    sequences = dict(source.execute('SELECT name,seq FROM sqlite_sequence'))
    for table, column in destination.connection.execute('''SELECT table_name,column_name
            FROM information_schema.columns WHERE table_schema=current_schema()
            AND is_identity='YES' '''):
        maximum = destination.connection.execute(sql.SQL('SELECT MAX({}) FROM {}').format(
            sql.Identifier(column), sql.Identifier(table))).fetchone()[0] or 0
        maximum = max(maximum, sequences.get(table, 0) if column != '_ab_order' else 0)
        name = destination.connection.execute('SELECT pg_get_serial_sequence(%s,%s)',
                                              (f'{destination.schema}.{table}', column)).fetchone()[0]
        destination.connection.execute('SELECT setval(%s,%s,%s)', (name, max(1, maximum), maximum > 0))


def fence(source):
    """An old SDK which ignores the new selector cannot resume canonical DML."""
    for table in tables(source):
        for action in ('INSERT', 'UPDATE', 'DELETE'):
            name = identifier(f'_ab_migrated_{table}_{action.lower()}')
            source.execute(f'CREATE TRIGGER IF NOT EXISTS {name} BEFORE {action} '
                           f'ON {identifier(table)} BEGIN '
                           "SELECT RAISE(ABORT,'AgentBridge Store migrated to PostgreSQL'); END")
