"""One-way, resumable SQLite to PostgreSQL migration under verified local exclusion."""

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3

from ..checkpoint import state
from ..errors import BridgeError
from .authority import guard
from .configuration import PostgresConfiguration, read_private
from .drain import legacy_locks, recorded_owners
from .migration_inventory import copy_rows, fence, inventory
from .postgres import PostgresBackend
from .schema import initialize, initialize_optional
from .selection import load, save

JOURNAL = '.store-migration.json'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


class Bootstrap:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        yield self.connection


def journal(root):
    try:
        return json.loads(read_private(root / JOURNAL))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise BridgeError('store_migration_corrupt', 'The private migration journal is invalid.') from None


def completed(connection, operation_id, source_digest):
    exists = connection.execute("SELECT 1 FROM information_schema.tables WHERE "
                                "table_schema=current_schema() AND table_name='store_migration_receipt'")
    if not exists.fetchone():
        return None
    row = connection.execute('SELECT operation_id,source_digest,result FROM store_migration_receipt')
    values = row.fetchall()
    if len(values) != 1 or values[0][0:2] != (operation_id, source_digest):
        raise BridgeError('store_migration_conflict', 'The destination belongs to another import.')
    return json.loads(values[0][2])


def import_store(source, postgres, record, verify):
    with PostgresBackend(postgres).connect() as destination:
        result = completed(destination, record['operation_id'], record['source_digest'])
        if result:
            if digest(inventory(destination, source, postgres=True)) != record['source_digest']:
                raise BridgeError('store_migration_corrupt', 'The imported Store has changed before activation.')
            verify()
            return result
        existing = destination.execute('SELECT 1 FROM information_schema.tables '
                                       'WHERE table_schema=current_schema() LIMIT 1').fetchone()
        if existing:
            raise BridgeError('store_migration_conflict', 'Use an empty, dedicated destination schema.')
        initialize(destination)
        initialize_optional(Bootstrap(destination))
        copy_rows(source, destination)
        copied = inventory(destination, source, postgres=True)
        if digest(copied) != record['source_digest']:
            raise BridgeError('store_migration_corrupt', 'Imported Store rows do not match the source.')
        result = {'format_version': '1', 'operation_id': record['operation_id'],
                  'state': 'migrated', 'backend': 'postgresql', **record['identity'],
                  'cursor': record['cursor'], 'source_digest': record['source_digest'],
                  'tables': len(copied), 'rows': sum(value['rows'] for value in copied.values())}
        destination.execute('CREATE TABLE IF NOT EXISTS store_migration_receipt('
                            'operation_id TEXT PRIMARY KEY,source_digest TEXT NOT NULL,'
                            'result TEXT NOT NULL)')
        destination.execute('INSERT INTO store_migration_receipt VALUES (?,?,?)',
                            (record['operation_id'], record['source_digest'], json.dumps(result)))
        verify()
        return result  # Context COMMIT includes every row and the receipt atomically.


def migrate_postgres(root, *, operation_id, owner_ref, postgres, verify_quiescence=None):
    """Host-only, local Linux migration. The host stops its owners before calling.

    Native Codex databases, proxy credential files and provider vaults remain in place.
    The verifier challenges owner/Store/generation, operation and root inode before copying
    and before SQL COMMIT. It must validate the host registry and closed PID namespace;
    a caller-supplied flag is not a process-domain verifier. Active work fails closed. This never kills or restarts work.
    A failed pending migration must be resumed with the same operation and configuration.
    """
    if not callable(verify_quiescence):
        raise BridgeError('store_migration_unsupported',
                          'A trusted host must verify the complete closed process domain.')
    state.canonical_uuid(operation_id)
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir() or not isinstance(postgres, PostgresConfiguration):
        raise BridgeError('invalid_store_configuration', 'Select an existing private Store root.')
    if postgres.conninfo_file.resolve().is_relative_to(root.resolve()):
        raise BridgeError('unsafe_store_configuration', 'Keep connection secrets outside the Store.')
    postgres.connection_parameters()
    desired = {'format_version': '1', 'backend': 'postgresql', **postgres.document()}
    with guard(root, exclusive=True):
        record, selected = journal(root), load(root)
        if record and (record.get('operation_id') != operation_id or
                       record.get('destination') != desired or
                       record.get('identity', {}).get('owner_ref') != owner_ref):
            raise BridgeError('store_migration_conflict', 'Resume the same private migration operation.')
        if selected == desired:
            if not record:
                raise BridgeError('store_migration_conflict', 'This destination was not migrated here.')
            with PostgresBackend(postgres).connect(read_only=True) as connection:
                result = completed(connection, operation_id, record['source_digest'])
                binding = state.identity(connection)
                if not result or any(binding[key] != result[key] for key in state.IDENTITY_KEYS):
                    raise BridgeError('store_migration_corrupt', 'The selected Store identity changed.')
            return result
        if selected and selected['backend'] == 'postgresql' and selected != {
                **desired, 'migration_operation': operation_id}:
            raise BridgeError('store_migration_conflict', 'This Store selected a different destination.')
        database = root / 'bridge.sqlite3'
        if database.is_symlink() or not database.is_file():
            raise BridgeError('store_migration_incompatible', 'An existing SQLite Store is required.')
        with legacy_locks(root), closing(sqlite3.connect(database, timeout=2)) as source:
            source.row_factory = sqlite3.Row
            try:
                source.execute('BEGIN EXCLUSIVE')
                recorded_owners(source)
                if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise BridgeError('store_migration_corrupt', 'The source Store is corrupt.')
                if source.execute('SELECT version FROM metadata').fetchone()[0] != 15:
                    raise BridgeError('store_migration_incompatible', 'Upgrade the Store schema first.')
                identity = state.identity(source)
                if identity['owner_ref'] != owner_ref or not identity['enabled']:
                    raise BridgeError('checkpoint_scope_mismatch', 'The Store owner does not match.')
                proof = {'format_version': '1', 'operation_id': operation_id,
                         **{key: identity[key] for key in state.IDENTITY_KEYS},
                         'root_binding': [root.stat().st_dev, root.stat().st_ino]}

                def verify():
                    actual = root.stat()
                    if ([actual.st_dev, actual.st_ino] != proof['root_binding']
                            or verify_quiescence(dict(proof)) is not True):
                        raise BridgeError('store_migration_busy',
                                          'The host process domain is not verifiably closed.')

                verify()
                source_digest = digest(inventory(source, source))
                if record and source_digest != record['source_digest']:
                    raise BridgeError('store_migration_corrupt', 'The frozen source Store changed.')
                if not record:
                    record = {'format_version': '1', 'operation_id': operation_id,
                              'destination': desired, 'source_digest': source_digest,
                              'identity': {key: identity[key] for key in state.IDENTITY_KEYS},
                              'cursor': state.cursor(source)}
                    save(root, record, filename=JOURNAL)
                fence(source)
                source.commit()
                # A legacy connection ignores the selector; the committed SQLite triggers
                # deny its DML permanently. New clients see pending before any PG write.
                save(root, {**desired, 'migration_operation': operation_id})
                source.execute('BEGIN EXCLUSIVE')
                result = import_store(source, postgres, record, verify)
                save(root, desired)
                return result
            except sqlite3.Error:
                raise BridgeError('store_migration_busy', 'The SQLite source could not be frozen.') from None
            finally:
                source.rollback()
