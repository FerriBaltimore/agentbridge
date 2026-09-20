"""Durable ownership of short-lived authentication workers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from .errors import BridgeError
from .process import alive, identity
from .security import base_environment


class AuthRuntime:
    def __init__(self, store):
        self.store = store
        self.children = []

    def get(self, attempt_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM auth_runtime WHERE attempt_id=?', (attempt_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value['config'] = json.loads(value['config'])
        return value

    @staticmethod
    def active(row):
        if not row or row['finished'] or not row['job_id']:
            return False
        if row['pid']:
            return alive(row['pid'], row['identity'])
        return time.time() - row['started'] < 15

    def reap(self):
        self.children = [child for child in self.children if child.poll() is None]

    def launch(self, attempt_id, kind):
        self.reap()
        token = uuid4().hex
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = dict(db.execute('SELECT * FROM auth_runtime WHERE attempt_id=?', (attempt_id,)).fetchone())
            if self.active(row):
                if row['kind'] == kind:
                    return False
                raise BridgeError('authentication_attempt_not_ready', 'The previous authentication step is still running.')
            db.execute('''UPDATE auth_runtime SET job_id=?,kind=?,pid=NULL,identity=NULL,
                started=?,finished=0 WHERE attempt_id=?''', (token, kind, time.time(), attempt_id))
        env = base_environment()
        env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)
        # Non-secret native executable configuration, never provider API keys.
        for name in ('GRANTBRIDGE_CODEX', 'GRANTBRIDGE_CLAUDE', 'GRANTBRIDGE_CHROME'):
            if name in os.environ:
                env[name] = os.environ[name]
        try:
            child = subprocess.Popen(
                [sys.executable, '-P', '-m', 'agentbridge.auth_worker', str(self.store.root), attempt_id, token],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env, start_new_session=True)
            self.children.append(child)
        except OSError:
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                owned = db.execute('UPDATE auth_runtime SET finished=1 WHERE attempt_id=? AND job_id=?',
                                   (attempt_id, token))
                if owned.rowcount and kind == 'start':
                    db.execute("UPDATE auth_attempts SET status='failed',data=?,updated=? WHERE id=? AND status='starting'",
                               (json.dumps({'error': {'code': 'launch_failed'}}), time.time(), attempt_id))
            raise BridgeError('launch_failed', 'Could not start the authentication worker.') from None
        return True

    def claim(self, attempt_id, token):
        with self.store.connect() as db:
            result = db.execute('''UPDATE auth_runtime SET pid=?,identity=?
                WHERE attempt_id=? AND job_id=? AND pid IS NULL AND finished=0''',
                                (os.getpid(), identity(os.getpid()), attempt_id, token))
        return result.rowcount == 1

    def finish(self, attempt_id, token):
        with self.store.connect() as db:
            db.execute('UPDATE auth_runtime SET finished=1 WHERE attempt_id=? AND job_id=?',
                       (attempt_id, token))

    def request_cancel(self, attempt_id):
        with self.store.connect() as db:
            db.execute('UPDATE auth_runtime SET cancel_requested=1 WHERE attempt_id=?', (attempt_id,))
