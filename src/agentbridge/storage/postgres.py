"""Isolated PostgreSQL Store transactions with commit-ordered per-Store admission."""

from contextlib import contextmanager
import hashlib
import re

from ..errors import BridgeError
from .sql_dialect import statements, translate


class StoreIntegrityError(BridgeError):
    def __init__(self):
        super().__init__('store_conflict', 'A Store uniqueness constraint rejected this change.')


class Row:
    def __init__(self, names, values):
        self.names, self.values = names, values

    def __getitem__(self, key):
        return self.values[key if isinstance(key, (int, slice)) else self.names.index(key)]

    def __iter__(self):
        return iter(self.values)

    def keys(self):
        return [name for name in self.names if name != '_ab_order']


def row_factory(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


class Result:
    def __init__(self, cursor, *, returning_id=False):
        self.cursor = cursor
        self.lastrowid = cursor.fetchone()[0] if returning_id else None

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)


class Connection:
    backend = 'postgresql'

    def __init__(self, connection, schema):
        self.connection, self.schema, self.locked = connection, schema, False
        self.lock_key = int.from_bytes(hashlib.sha256(('agentbridge:' + schema).encode()).digest()[:8],
                                       'big', signed=True)

    @property
    def in_transaction(self):
        return self.locked

    def _lock(self):
        if not self.locked:
            self.connection.execute('SELECT pg_advisory_xact_lock(%s)', (self.lock_key,))
            self.locked = True

    def execute(self, sql, parameters=()):
        import psycopg

        try:
            command = sql.strip().rstrip(';')
            if command.upper() in {'BEGIN', 'BEGIN IMMEDIATE'}:
                self._lock()
                return self
            if command.upper() == 'COMMIT':
                self.commit()
                return self
            self._lock()
            columns = re.fullmatch(r'PRAGMA table_info\((\w+)\)', command, re.I)
            if columns:
                cursor = self.connection.execute('''SELECT ordinal_position - 1 AS cid,
                    column_name AS name, data_type AS type
                    FROM information_schema.columns
                    WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position''',
                    (self.schema, columns[1]))
                return Result(cursor)
            indexes = re.fullmatch(r'PRAGMA index_list\((\w+)\)', command, re.I)
            if indexes:
                return Result(self.connection.execute(
                    'SELECT indexname AS name FROM pg_indexes WHERE schemaname=%s AND tablename=%s',
                    (self.schema, indexes[1])))
            if command.upper().startswith('PRAGMA'):
                raise BridgeError('postgres_sql_unsupported',
                                  'SQLite maintenance does not apply to PostgreSQL.')
            returning_id = bool(re.match(r'INSERT INTO events\s*\(', command, re.I))
            translated = translate(command)
            if returning_id:
                translated += ' RETURNING seq'
            return Result(self.connection.execute(translated, parameters), returning_id=returning_id)
        except psycopg.IntegrityError:
            raise StoreIntegrityError() from None
        except psycopg.Error as error:
            raise BridgeError('postgres_store_failed', 'The PostgreSQL Store operation failed.',
                              retryable=True, details={'sqlstate': error.sqlstate}) from None

    def executemany(self, sql, parameters):
        import psycopg

        try:
            self._lock()
            cursor = self.connection.cursor()
            cursor.executemany(translate(sql), parameters)
            return Result(cursor)
        except psycopg.IntegrityError:
            raise StoreIntegrityError() from None
        except psycopg.Error as error:
            raise BridgeError('postgres_store_failed', 'The PostgreSQL Store operation failed.',
                              retryable=True, details={'sqlstate': error.sqlstate}) from None

    def executescript(self, script):
        for statement in statements(script):
            self.execute(statement)

    def commit(self):
        self.connection.commit()
        self.locked = False

    def rollback(self):
        self.connection.rollback()
        self.locked = False


class PostgresBackend:
    name, path = 'postgresql', None

    def __init__(self, configuration):
        self.configuration = configuration

    @contextmanager
    def connect(self):
        try:
            import psycopg
            from psycopg import sql
        except ImportError:
            raise BridgeError('postgres_dependency_required',
                              'Install AgentBridge with its postgres extra.') from None

        connection = None
        try:
            connection = psycopg.connect(**self.configuration.connection_parameters(),
                                         row_factory=row_factory)
            role = connection.execute('''SELECT r.rolsuper,r.rolcreatedb,r.rolcreaterole,
                r.rolreplication,r.rolbypassrls,n.nspowner=r.oid,
                EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid),
                EXISTS(SELECT 1 FROM pg_namespace other WHERE other.nspowner=r.oid
                       AND other.oid<>n.oid),
                EXISTS(SELECT 1 FROM pg_database d WHERE d.datdba=r.oid)
                FROM pg_roles r JOIN pg_namespace n ON n.nspname=%s
                WHERE r.rolname=current_user''', (self.configuration.schema,)).fetchone()
            if not role or any(role[index] for index in range(5)) or not role[5] or any(role[index] for index in (6, 7, 8)):
                raise BridgeError('postgres_scope_denied',
                                  'Use a non-administrative role owning only its Store schema.')
            connection.execute(sql.SQL('SET search_path TO {}, pg_catalog').format(
                sql.Identifier(self.configuration.schema)))
            connection.commit()
            wrapped = Connection(connection, self.configuration.schema)
            yield wrapped
            wrapped.commit()
        except psycopg.Error as error:
            raise BridgeError('postgres_store_unavailable', 'The PostgreSQL Store is unavailable.',
                              retryable=True, details={'sqlstate': error.sqlstate}) from None
        finally:
            if connection is not None:
                connection.close()
