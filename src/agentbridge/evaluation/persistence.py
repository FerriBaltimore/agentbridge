"""Atomic deletion and idempotent receipt for one disposable evaluation."""

import shutil
import time

from ..errors import BridgeError
from ..models import TERMINAL, identifier
from ..process import alive


def _table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (name,)).fetchone() is not None


def _process_may_run(run):
    if run['state'] not in TERMINAL:
        return True
    for prefix in ('worker', 'child'):
        pid, token = run[f'{prefix}_pid'], run[f'{prefix}_identity']
        if pid is not None and (not token or alive(pid, token)):
            return True
    return False


class EvaluationStoreMixin:
    def _remove_evaluation_home(self, instance_id):
        runtime = self.root / 'codex-runtime'
        if runtime.is_symlink():
            raise BridgeError('unsafe_store', 'The Codex runtime directory is unsafe.')
        if not runtime.exists():
            return
        if not runtime.is_dir() or runtime.resolve() != runtime:
            raise BridgeError('unsafe_store', 'The Codex runtime directory is unsafe.')
        home = runtime / instance_id
        if home.is_symlink():
            raise BridgeError('unsafe_store', 'The evaluation home is unsafe.')
        if not home.exists():
            return
        if not home.is_dir() or not shutil.rmtree.avoids_symlink_attacks:
            raise BridgeError('unsafe_store', 'The evaluation home is unsafe.')
        try:
            shutil.rmtree(home)
        except OSError:
            raise BridgeError('evaluation_cleanup_failed',
                              'The evaluation home could not be removed; retry discard.') from None

    def discard_evaluation(self, instance_id, *, account_id=None):
        """Purge only marked evaluations after all owned processes have exited."""
        identifier(instance_id)
        with self.connect() as db:
            db.execute('PRAGMA secure_delete=ON')
            db.execute('BEGIN IMMEDIATE')
            marker = db.execute('SELECT * FROM evaluation_instances WHERE session_id=?',
                                (instance_id,)).fetchone()
            if marker is None:
                raise BridgeError('evaluation_required',
                                  'Only an explicitly created evaluation can be discarded.')
            if account_id is not None and marker['account_id'] != account_id:
                raise BridgeError('account_mismatch', 'The evaluation belongs to another account.')
            if marker['status'] == 'discarded':
                return {'instance_id': instance_id, 'discarded': True, 'pending': False}
            if marker['status'] == 'active':
                runs = db.execute('SELECT * FROM runs WHERE session_id=?',
                                  (instance_id,)).fetchall()
                if any(_process_may_run(run) for run in runs):
                    return {'instance_id': instance_id, 'discarded': False, 'pending': True}
                db.execute("UPDATE evaluation_instances SET status='discarding' WHERE session_id=?",
                           (instance_id,))
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
                           ('{"evaluation_discarded":true}', instance_id))
        self._remove_evaluation_home(instance_id)
        with self.connect() as db:
            checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint is None or checkpoint[0] != 0:
            return {'instance_id': instance_id, 'discarded': False, 'pending': True}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE evaluation_instances SET status='discarded',discarded_at=? "
                       "WHERE session_id=? AND status='discarding'",
                       (time.time(), instance_id))
        return {'instance_id': instance_id, 'discarded': True, 'pending': False}
