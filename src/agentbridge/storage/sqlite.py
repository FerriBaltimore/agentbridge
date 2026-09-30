"""Standalone SQLite authority, retaining native backup and secure-delete behavior."""

from contextlib import contextmanager
import sqlite3

from ..errors import BridgeError


class SQLiteBackend:
    name = 'sqlite'

    def __init__(self, root):
        self.path = root / 'bridge.sqlite3'
        if self.path.is_symlink():
            raise BridgeError('unsafe_store', 'Database cannot be a symbolic link.')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def prepare_deletion(connection):
    if isinstance(connection, sqlite3.Connection):
        connection.execute('PRAGMA secure_delete=ON')


def finish_deletion(connection):
    if isinstance(connection, sqlite3.Connection):
        row = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        return row is not None and row[0] == 0
    # PostgreSQL deletion is logical and committed. MVCC, WAL and retained backups
    # remain subject to the operator's retention policy; never claim secure erasure.
    return True
