"""Durable explicit backend selection; failures never fall back to another authority."""

import fcntl
import json
import os
import stat
from pathlib import Path
import tempfile

from ..errors import BridgeError
from .configuration import PostgresConfiguration, read_private
from .sqlite import SQLiteBackend

FILENAME = 'store-backend.json'


def load(root):
    try:
        value = json.loads(read_private(Path(root) / FILENAME))
    except FileNotFoundError:
        return None
    except BridgeError:
        raise
    except (OSError, ValueError):
        raise BridgeError('invalid_store_configuration', 'Backend selection is unreadable.') from None
    if not isinstance(value, dict):
        raise BridgeError('invalid_store_configuration', 'Backend selection is invalid.')
    fields = {'format_version', 'backend'}
    if value.get('backend') == 'postgresql':
        fields |= {'schema', 'conninfo_file'}
        for pending in ('migration_operation', 'recovery_operation'):
            if pending in value:
                fields.add(pending)
                from ..checkpoint.state import canonical_uuid

                canonical_uuid(value[pending])
        if 'physical_guard' in value:
            fields.add('physical_guard')
            if type(value['physical_guard']) is not int:
                raise BridgeError('invalid_store_configuration', 'Physical guard is invalid.')
    if (set(value) != fields or value.get('format_version') != '1'
            or value.get('backend') not in {'sqlite', 'postgresql'}):
        raise BridgeError('invalid_store_configuration', 'Backend selection is invalid.')
    return value


def save(root, value, *, filename=FILENAME):
    descriptor, temporary = tempfile.mkstemp(prefix='.backend-', dir=root)
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(value, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, root / filename)
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def select(root, *, backend=None, postgres=None):
    from .authority import guard

    with guard(root):
        return select_locked(root, backend=backend, postgres=postgres)


def select_locked(root, *, backend=None, postgres=None):
    if backend not in {None, 'sqlite', 'postgresql'}:
        raise BridgeError('invalid_store_configuration', 'Select sqlite or postgresql explicitly.')
    if postgres is not None and (backend != 'postgresql'
                                 or not isinstance(postgres, PostgresConfiguration)):
        raise BridgeError('invalid_store_configuration', 'PostgreSQL requires explicit selection.')
    lock = os.open(root / '.store-backend.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BridgeError('unsafe_store_configuration', 'Backend lock must be private.')
        fcntl.flock(lock, fcntl.LOCK_EX)
        saved = load(root)
        from .authority import require_ready

        require_ready(saved)
        selected = backend or (saved['backend'] if saved else 'sqlite')
        if saved and selected != saved['backend']:
            raise BridgeError('store_migration_required', 'Migrate the Store before changing backend.')
        if selected == 'postgresql':
            if (root / 'bridge.sqlite3').exists() and not saved:
                raise BridgeError('store_migration_required', 'Migrate existing SQLite state first.')
            if postgres is None:
                if not saved:
                    raise BridgeError('invalid_store_configuration', 'PostgreSQL configuration is required.')
                postgres = PostgresConfiguration(saved['schema'], Path(saved['conninfo_file']),
                                                  saved.get('physical_guard'))
            if postgres.conninfo_file.resolve().is_relative_to(root.resolve()):
                raise BridgeError('unsafe_store_configuration', 'Keep connection secrets outside the Store.')
            desired = {'format_version': '1', 'backend': selected, **postgres.document()}
            if saved and desired != saved:
                raise BridgeError('store_migration_required', 'Connection rebinding requires host recovery.')
            if not saved:
                postgres.connection_parameters()  # Validate privately before publishing the selection.
                save(root, desired)
            from .postgres import PostgresBackend

            return PostgresBackend(postgres)
        if not saved:
            save(root, {'format_version': '1', 'backend': 'sqlite'})
        return SQLiteBackend(root)
    finally:
        os.close(lock)
