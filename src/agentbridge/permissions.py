"""One-use approval decisions persisted before delivery to the live provider."""
import json
import math
import time
from uuid import uuid4

from .errors import BridgeError
from .models import TERMINAL, identifier
from .store import dumps


class Permissions:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE IF NOT EXISTS permission_requests(
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, request TEXT NOT NULL,
                expires REAL NOT NULL, decision TEXT, reason TEXT, delivered INTEGER NOT NULL DEFAULT 0,
                applied_decision TEXT)''')
            if 'applied_decision' not in {row[1] for row in db.execute('PRAGMA table_info(permission_requests)')}:
                db.execute('ALTER TABLE permission_requests ADD COLUMN applied_decision TEXT')

    def _event(self, db, run, kind, data):
        db.execute('INSERT INTO events(run_id,session_id,kind,at,data) VALUES (?,?,?,?,?)',
                   (run['id'], run['session_id'], kind, time.time(), dumps(data)))

    def request(self, turn_id, details, *, timeout):
        if not isinstance(details, dict):
            raise BridgeError('invalid_permissions', 'Permission details must be an object.')
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or timeout < 0):
            raise BridgeError('invalid_timeout', 'Permission timeout must be finite and nonnegative.')
        permission_id, expires = uuid4().hex, time.time() + min(timeout, 600)
        run = self.store.get('runs', identifier(turn_id))
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO permission_requests(id,run_id,request,expires) VALUES (?,?,?,?)',
                       (permission_id, turn_id, dumps(details), expires))
            self._event(db, run, 'permission_required',
                        {**details, 'permission_id': permission_id, 'expires_at': expires})
        return permission_id

    def respond(self, turn_id, permission_id, decision, *, reason=None, expires_at=None):
        if decision not in {'allow', 'deny'}:
            raise BridgeError('invalid_permissions', 'Choose allow or deny for this request only.')
        if reason is not None and (not isinstance(reason, str) or len(reason) > 1000):
            raise BridgeError('invalid_permissions', 'The permission reason must be bounded text.')
        if expires_at is not None and (not isinstance(expires_at, (float, int)) or isinstance(expires_at, bool)
                                       or not math.isfinite(expires_at) or expires_at <= time.time()):
            raise BridgeError('permission_expired', 'The permission response has expired.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM permission_requests WHERE id=? AND run_id=?',
                             (identifier(permission_id), identifier(turn_id))).fetchone()
            if row is None:
                raise BridgeError('not_found', 'No matching permission request exists for this turn.')
            if row['decision']:
                if row['decision'] != decision or row['reason'] != reason:
                    raise BridgeError('idempotency_conflict', 'This permission already has a different decision.')
                return {'permission_id': permission_id, 'decision': decision,
                        'state': 'delivered' if row['delivered'] else 'queued',
                        'applied_decision': row['applied_decision'], 'replayed': True}
            run = db.execute('SELECT * FROM runs WHERE id=?', (turn_id,)).fetchone()
            if row['expires'] <= time.time() or run['state'] in TERMINAL or run['stop_requested']:
                raise BridgeError('permission_expired', 'This permission request is no longer active.')
            expires = min(row['expires'], expires_at) if expires_at is not None else row['expires']
            db.execute('UPDATE permission_requests SET decision=?,reason=?,expires=? WHERE id=?',
                       (decision, reason, expires, permission_id))
            self._event(db, run, 'permission_response',
                        {'permission_id': permission_id, 'decision': decision, 'state': 'queued'})
        return {'permission_id': permission_id, 'decision': decision, 'state': 'queued', 'replayed': False}

    def wait(self, turn_id, permission_id):
        while True:
            with self.store.connect() as db:
                row = db.execute('SELECT * FROM permission_requests WHERE id=? AND run_id=?',
                                 (permission_id, turn_id)).fetchone()
            run = self.store.get('runs', turn_id)
            if not row or row['expires'] <= time.time() or run['stop_requested'] or run['state'] in TERMINAL:
                return 'deny'
            if row['decision']:
                return row['decision']
            time.sleep(0.05)

    def delivered(self, turn_id, permission_id, decision):
        if decision not in {'allow', 'deny'}:
            raise BridgeError('invalid_permissions', 'Only an observed allow or deny can be recorded.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM permission_requests WHERE id=? AND run_id=?',
                             (identifier(permission_id), identifier(turn_id))).fetchone()
            if row is None:
                raise BridgeError('not_found', 'No matching permission request exists for this turn.')
            if row['delivered']:
                if row['applied_decision'] != decision:
                    raise BridgeError('idempotency_conflict', 'This permission has another observed decision.')
                return
            db.execute('UPDATE permission_requests SET delivered=1,applied_decision=? WHERE id=? AND run_id=?',
                       (decision, permission_id, turn_id))
            run = db.execute('SELECT * FROM runs WHERE id=?', (turn_id,)).fetchone()
            self._event(db, run, 'permission_response',
                        {'permission_id': permission_id, 'decision': decision, 'state': 'delivered'})
