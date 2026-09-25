"""Delete a durable conversation and its private local execution evidence."""

import time
import re
import sqlite3

from .errors import BridgeError
from .evaluation.persistence import _process_may_run, _table_exists
from .models import identifier
from .proxy.home import remove_session_home


def migrate_v9(db, version):
    if version != 8:
        return version
    db.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS deleted_instances(
            session_id TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK(status IN ('deleting','deleted')),
            deleted_at REAL);
        UPDATE metadata SET version=9;
        COMMIT;
    ''')
    return 9


def remove_instance_archives(store_root, instance_id):
    """Remove exported evidence and interrupted writes for exactly one ID."""
    folder = store_root / 'archives'
    if folder.is_symlink():
        raise BridgeError('unsafe_store', 'The archive directory is unsafe.')
    if not folder.exists():
        return
    if not folder.is_dir() or folder.resolve() != folder:
        raise BridgeError('unsafe_store', 'The archive directory is unsafe.')
    archive_name = re.compile(rf'{re.escape(instance_id)}-[0-9a-f]{{16}}\.jsonl\Z')
    stage_prefix = f'.staged-{instance_id}~'
    try:
        for path in folder.iterdir():
            if archive_name.fullmatch(path.name) or path.name.startswith(stage_prefix):
                path.unlink(missing_ok=True)
    except OSError:
        raise BridgeError('instance_cleanup_failed',
                          'The instance archive could not be removed; retry deletion.') from None


class InstanceDeletionStoreMixin:
    def recover_deleting_instances(self, *, limit=100):
        """Finish a bounded number of previously authorized local deletions."""
        with self.connect() as db:
            rows = db.execute("SELECT session_id FROM deleted_instances WHERE status='deleting' "
                              "ORDER BY rowid LIMIT ?", (limit,)).fetchall()
        for row in rows:
            try:
                self.delete_instance(row['session_id'])
            except (BridgeError, OSError, sqlite3.Error):
                # The exact ID remains retriable after an unsafe or unavailable path.
                continue

    def delete_instance(self, instance_id, *, expected_version=None):
        """Purge a conversation after its turn and owned processes have ended."""
        identifier(instance_id)
        with self.connect() as db:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('BEGIN IMMEDIATE')
            marker = db.execute('SELECT status FROM deleted_instances WHERE session_id=?',
                                (instance_id,)).fetchone()
            if marker is None:
                instance = db.execute('SELECT 1 FROM sessions WHERE id=?',
                                      (instance_id,)).fetchone()
                if instance is None:
                    raise BridgeError('instance_not_found', 'Instance does not exist.')
                if db.execute('SELECT 1 FROM evaluation_instances WHERE session_id=?',
                              (instance_id,)).fetchone():
                    raise BridgeError('evaluation_discard_required',
                                      'Discard evaluation instances with instances.discard_evaluation.')
                metadata = db.execute('SELECT version FROM instance_metadata WHERE session_id=?',
                                      (instance_id,)).fetchone()
                version = metadata['version'] if metadata else 1
                if expected_version is not None and expected_version != version:
                    raise BridgeError('version_conflict', 'Instance changed since it was read.')
                runs = db.execute('SELECT * FROM runs WHERE session_id=?',
                                  (instance_id,)).fetchall()
                if any(_process_may_run(run) for run in runs):
                    raise BridgeError('busy', 'A turn or owned process is still running.')

                db.execute('INSERT INTO deleted_instances(session_id,status) VALUES (?,?)',
                           (instance_id, 'deleting'))
                db.execute('UPDATE sessions SET parent_id=NULL WHERE parent_id=?',
                           (instance_id,))
                db.execute('DELETE FROM run_route_exclusions WHERE run_id IN '
                           '(SELECT id FROM runs WHERE session_id=?)', (instance_id,))
                if _table_exists(db, 'permission_requests'):
                    db.execute('DELETE FROM permission_requests WHERE run_id IN '
                               '(SELECT id FROM runs WHERE session_id=?)', (instance_id,))
                if _table_exists(db, 'run_contracts'):
                    db.execute('DELETE FROM run_contracts WHERE run_id IN '
                               '(SELECT id FROM runs WHERE session_id=?)', (instance_id,))
                if _table_exists(db, 'error_cases'):
                    for column in ('first_turn_id', 'last_turn_id'):
                        db.execute(f'UPDATE error_cases SET {column}=NULL WHERE {column} IN '
                                   '(SELECT id FROM runs WHERE session_id=?)', (instance_id,))
                db.execute('DELETE FROM events WHERE session_id=?', (instance_id,))
                db.execute('DELETE FROM runs WHERE session_id=?', (instance_id,))
                db.execute('DELETE FROM session_routing WHERE session_id=?', (instance_id,))
                db.execute('DELETE FROM instance_metadata WHERE session_id=?', (instance_id,))
                db.execute('DELETE FROM sessions WHERE id=?', (instance_id,))
                db.execute('UPDATE instance_requests SET payload=? WHERE session_id=?',
                           ('{"instance_deleted":true}', instance_id))

        remove_session_home(self.root, instance_id, error_code='instance_cleanup_failed')
        remove_instance_archives(self.root, instance_id)
        with self.connect() as db:
            checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            return {'instance_id': instance_id, 'deleted': False, 'pending': True}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE deleted_instances SET status='deleted',deleted_at=? "
                       "WHERE session_id=? AND status='deleting'", (time.time(), instance_id))
        return {'instance_id': instance_id, 'deleted': True, 'pending': False}


class InstanceDeletionMixin:
    def instance_delete(self, instance_id, *, expected_version=None):
        result = self.store.delete_instance(instance_id, expected_version=expected_version)
        if result['deleted']:
            for turn_id, process in list(self._children.items()):
                if process.poll() is not None:
                    process.wait()
                    self._children.pop(turn_id, None)
        return result
