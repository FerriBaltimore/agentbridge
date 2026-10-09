"""Host rebinding of an already recovered physical PostgreSQL Store, always held."""

import json
from pathlib import Path
from uuid import uuid4

from . import native, state
from .recovery import hold_import
from ..storage.authority import guard
from ..storage.configuration import PostgresConfiguration, read_private
from ..storage.migration import digest
from ..storage.observation import observe_connection
from ..storage.postgres import PostgresBackend
from ..storage.selection import load, save

JOURNAL = '.store-recovery.json'


def restore_postgres(destination, snapshot, *, postgres, owner_ref, operation_id,
                     workspace_paths=None):
    """Host must authenticate the manifest and restore its exact physical SQL frontier first.

    Never reuse a copied conninfo_file as authority: postgres is newly provisioned by the
    destination host. This API checks the recovered rows against the captured observation;
    it does not perform SQL recovery or attest a caller-supplied frontier's protection.
    """
    state.canonical_uuid(operation_id)
    if (snapshot.get('format_version') != '1' or snapshot.get('backend') != 'postgresql'
            or snapshot.get('owner_ref') != owner_ref or snapshot.get('store_schema') not in {15, 16}
            or 'sqlite_backup' in snapshot or 'content' in snapshot
            or not isinstance(postgres, PostgresConfiguration)):
        native.fail('checkpoint_incompatible')
    state.canonical_uuid(snapshot.get('sql_frontier_id'))
    destination = Path(destination).absolute()
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        native.fail('checkpoint_unsafe_path')
    if postgres.conninfo_file.resolve().is_relative_to(destination.resolve()):
        native.fail('checkpoint_unsafe_path')
    postgres.connection_parameters()
    expected = {'format_version': '1', 'backend': 'postgresql', **postgres.document()}
    payload = digest({'snapshot': snapshot, 'destination': expected,
                      'workspaces': {key: str(value) for key, value in (workspace_paths or {}).items()}})
    if not destination.exists():
        native.private_directory(destination)
    with guard(destination, exclusive=True):
        try:
            record = json.loads(read_private(destination / JOURNAL))
        except FileNotFoundError:
            if {item.name for item in destination.iterdir()} != {'.store-authority.lock'}:
                native.fail('checkpoint_scope_mismatch')
            record = {'operation_id': operation_id, 'payload': payload, 'generation': str(uuid4())}
            save(destination, record, filename=JOURNAL)
        except (OSError, ValueError):
            native.fail('checkpoint_scope_mismatch')
        if record.get('operation_id') != operation_id or record.get('payload') != payload:
            native.fail('checkpoint_scope_mismatch')
        selected = load(destination)
        pending = {**expected, 'recovery_operation': operation_id}
        if selected not in (None, expected, pending):
            native.fail('checkpoint_scope_mismatch')
        if selected != expected:
            save(destination, pending)
        with PostgresBackend(postgres).connect() as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS store_recovery_receipt('
                               'operation_id TEXT PRIMARY KEY,payload TEXT NOT NULL,result TEXT NOT NULL)')
            row = connection.execute('SELECT payload,result FROM store_recovery_receipt '
                                     'WHERE operation_id=?', (operation_id,)).fetchone()
            if row:
                result = json.loads(row[1])
                binding = state.identity(connection)
                if (row[0] != payload or binding['store_generation'] != record['generation']
                        or binding['owner_ref'] != owner_ref or not binding['recovery_held']):
                    native.fail('checkpoint_scope_mismatch')
            else:
                params = {key: snapshot[key] for key in state.IDENTITY_KEYS}
                observed = observe_connection(connection, 'postgresql',
                                              format_version='1', params=params)
                if any(observed[key] != snapshot[key] for key in
                       (*state.IDENTITY_KEYS, 'backend', 'store_schema', 'cursor', 'coverage')):
                    native.fail('checkpoint_corrupt')
                if state.migrate_mode(connection, snapshot['store_schema']) != 16:
                    native.fail('checkpoint_incompatible')
                hold_import(connection, destination, snapshot, record['generation'], workspace_paths)
                result = {'store_id': snapshot['store_id'], 'owner_ref': owner_ref,
                          'store_generation': record['generation'], 'recovery_held': True,
                          'source_cursor': snapshot['cursor'],
                          'replay_start': state.cursor(connection, snapshot['cursor']['seq'])}
                connection.execute('INSERT INTO store_recovery_receipt VALUES (?,?,?)',
                                   (operation_id, payload, json.dumps(result)))
        save(destination, expected)
        return result
