"""Inspect the SDK-owned Store layout without initializing or migrating any database."""

from contextlib import closing
import os
from pathlib import Path
import stat
import sqlite3

from ..errors import BridgeError


def inspect(root):
    root = Path(root).absolute()
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise BridgeError('native_identity_unknown', 'The Store location cannot be verified.')
    if not root.exists():
        return {'state': 'absent'}
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BridgeError('native_identity_unknown', 'The Store location cannot be verified.')
    from ..storage.inspection import inspect_postgres

    if selected := inspect_postgres(root):
        return selected
    database = root / 'bridge.sqlite3'
    if not database.exists():
        if any(root.iterdir()):
            raise BridgeError('native_identity_unknown', 'The Store layout is not recognized.')
        return {'state': 'absent'}
    info = database.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BridgeError('native_identity_unknown', 'The Store database cannot be verified.')
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
            connection.execute('PRAGMA query_only=ON')
            rows = connection.execute('SELECT version FROM metadata LIMIT 2').fetchall()
            if len(rows) != 1 or type(rows[0][0]) is not int:
                raise BridgeError('native_identity_unknown', 'The Store schema cannot be read.')
            version = rows[0][0]
            if version > 15:
                raise BridgeError('runtime_incompatible', 'The Store schema is not supported.')
            if version < 15:
                raise BridgeError('native_identity_unknown', 'The legacy Store needs host migration.')
            enabled = connection.execute('SELECT enabled FROM store_identity WHERE singleton=1')
            row = enabled.fetchone()
            if row is None or row[0] != 1:
                raise BridgeError('native_identity_unknown', 'Durable Store identity is not enabled.')
    except sqlite3.Error:
        raise BridgeError('native_identity_unknown', 'The Store identity cannot be read.') from None
    return {'state': 'present'}
