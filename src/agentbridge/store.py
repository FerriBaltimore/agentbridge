"""SQLite is the durable authority. A single writer transaction admits each run."""
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import sqlite3
import time

from .errors import BridgeError, BusyError
from .models import Event, TERMINAL


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "bridge.sqlite3"
        if self.path.is_symlink():
            raise BridgeError("unsafe_store", "Database cannot be a symbolic link.")
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata(version INTEGER NOT NULL);
                INSERT INTO metadata SELECT 2 WHERE NOT EXISTS(SELECT 1 FROM metadata);
                CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    cwd TEXT NOT NULL, model TEXT, native_id TEXT, parent_id TEXT, context TEXT,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    account_id TEXT NOT NULL, state TEXT NOT NULL, prompt TEXT NOT NULL,
                    options TEXT NOT NULL, request_key TEXT UNIQUE, created REAL NOT NULL,
                    updated REAL NOT NULL, worker_pid INTEGER, worker_identity TEXT,
                    child_pid INTEGER, child_identity TEXT, stop_requested INTEGER DEFAULT 0,
                    error TEXT, exit_code INTEGER);
                CREATE UNIQUE INDEX IF NOT EXISTS active_session ON runs(session_id)
                    WHERE state IN ('starting','running','stopping');
                CREATE UNIQUE INDEX IF NOT EXISTS active_account ON runs(account_id)
                    WHERE state IN ('starting','running','stopping');
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL, session_id TEXT NOT NULL, kind TEXT NOT NULL,
                    at REAL NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_run ON events(run_id,seq);
                CREATE INDEX IF NOT EXISTS event_session ON events(session_id,seq);
                CREATE TABLE IF NOT EXISTS account_observations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL, observed_at REAL NOT NULL,
                    source TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS account_observation_latest
                    ON account_observations(account_id, observed_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS usage_observations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL, observed_at REAL NOT NULL,
                    source TEXT NOT NULL, scope TEXT NOT NULL,
                    stale INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS usage_observation_latest
                    ON usage_observations(account_id, scope, observed_at DESC, id DESC);
            ''')
            version = db.execute('SELECT version FROM metadata').fetchone()[0]
            if version == 1:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS account_observations(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_id TEXT NOT NULL, observed_at REAL NOT NULL,
                        source TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS account_observation_latest
                        ON account_observations(account_id, observed_at DESC, id DESC);
                    CREATE TABLE IF NOT EXISTS usage_observations(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        account_id TEXT NOT NULL, observed_at REAL NOT NULL,
                        source TEXT NOT NULL, scope TEXT NOT NULL,
                        stale INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS usage_observation_latest
                        ON usage_observations(account_id, scope, observed_at DESC, id DESC);
                    UPDATE metadata SET version=2;
                ''')
                version = 2
            if version != 2:
                raise BridgeError("schema_version", "This store needs a different AgentBridge version.")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, table, id):
        if table not in ('runs','sessions','accounts'):
            raise ValueError(table)
        with self.connect() as db:
            row = db.execute(f'SELECT * FROM {table} WHERE id=?', (id,)).fetchone()
        if row is None:
            raise BridgeError('not_found', f'{table} record does not exist.')
        return dict(row)

    def list(self, table):
        if table not in ('runs','sessions','accounts'):
            raise ValueError(table)
        with self.connect() as db:
            rows = db.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
        return [dict(r) for r in rows]

    def account_observation(self, account_id, source, status, data, *, observed_at=None):
        with self.connect() as db:
            db.execute('INSERT INTO account_observations(account_id,observed_at,source,status,data) VALUES (?,?,?,?,?)',
                       (account_id, observed_at or time.time(), source, status, dumps(data)))

    def latest_account_observation(self, account_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM account_observations WHERE account_id=? ORDER BY observed_at DESC,id DESC LIMIT 1',
                             (account_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result['data'] = json.loads(result['data'])
        return result

    def usage_observation(self, account_id, source, scope, data, *, stale=False, observed_at=None):
        with self.connect() as db:
            db.execute('INSERT INTO usage_observations(account_id,observed_at,source,scope,stale,data) VALUES (?,?,?,?,?,?)',
                       (account_id, observed_at or time.time(), source, scope, int(stale), dumps(data)))

    def latest_usage_observation(self, account_id, scope='account'):
        with self.connect() as db:
            row = db.execute('SELECT * FROM usage_observations WHERE account_id=? AND scope=? ORDER BY observed_at DESC,id DESC LIMIT 1',
                             (account_id, scope)).fetchone()
        if not row:
            return None
        result = dict(row)
        result['data'] = json.loads(result['data'])
        result['stale'] = bool(result['stale'])
        return result

    def usage_history(self, account_id, scope='account', limit=100):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM usage_observations WHERE account_id=? AND scope=? ORDER BY observed_at DESC,id DESC LIMIT ?',
                              (account_id, scope, limit)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['data'] = json.loads(item['data'])
            item['stale'] = bool(item['stale'])
            result.append(item)
        return result

    def account(self, account):
        config = dumps(account.to_dict())
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT config FROM accounts WHERE id=?', (account.id,)).fetchone()
            if old and json.loads(old[0]) != json.loads(config):
                raise BridgeError('account_changed', 'Account IDs are immutable. Register a new ID for a different identity.')
            if account.home:
                for row in db.execute('SELECT id,config FROM accounts'):
                    other = json.loads(row['config'])
                    if row['id'] != account.id and other.get('home') == account.home and other['engine'] == account.engine:
                        raise BridgeError('home_in_use', 'This native account home already has an ID.')
            db.execute('INSERT OR IGNORE INTO accounts VALUES (?,?)', (account.id, config))

    def add_session(self, id, account_id, cwd, model, native_id=None, parent_id=None, context=None):
        with self.connect() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)',
                       (id, account_id, cwd, model, native_id, parent_id, context, time.time()))

    def admit(self, id, session_id, prompt, options, key):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if key:
                old = db.execute('SELECT * FROM runs WHERE request_key=?',(key,)).fetchone()
                if old:
                    if (old['session_id'],old['prompt'],old['options']) != (session_id,prompt,dumps(asdict(options))):
                        raise BridgeError('idempotency_conflict', 'Request key already belongs to different input.')
                    return old['id'], False
            session = db.execute('SELECT * FROM sessions WHERE id=?',(session_id,)).fetchone()
            if not session:
                raise BridgeError('not_found', 'Session does not exist.')
            now = time.time()
            try:
                db.execute('INSERT INTO runs(id,session_id,account_id,state,prompt,options,request_key,created,updated) VALUES (?,?,?,?,?,?,?,?,?)',
                           (id,session_id,session['account_id'],'starting',prompt,dumps(asdict(options)),key,now,now))
            except sqlite3.IntegrityError as e:
                raise BusyError() from e
            self._event(db,id,session_id,'user',{'text':prompt})
        return id, True

    def claim(self, id, pid, identity):
        with self.connect() as db:
            cursor = db.execute("UPDATE runs SET state='running',worker_pid=?,worker_identity=?,updated=? WHERE id=? AND state='starting' AND worker_pid IS NULL",
                                (pid,identity,time.time(),id))
            return cursor.rowcount == 1

    def update(self, id, **values):
        allowed = {'updated','child_pid','child_identity','state','error','exit_code'}
        if not values or not values.keys() <= allowed:
            raise ValueError('Invalid update fields')
        with self.connect() as db:
            db.execute(f"UPDATE runs SET {','.join(k+'=?' for k in values)} WHERE id=?",(*values.values(),id))

    def emit(self, id, kind, data):
        with self.connect() as db:
            row = db.execute('SELECT session_id FROM runs WHERE id=?',(id,)).fetchone()
            self._event(db,id,row[0],kind,data)
            if kind == 'session' and data.get('native_id'):
                db.execute('UPDATE sessions SET native_id=? WHERE id=?',(data['native_id'],row[0]))

    @staticmethod
    def _event(db, run_id, session_id, kind, data):
        db.execute('INSERT INTO events(run_id,session_id,kind,at,data) VALUES (?,?,?,?,?)',
                   (run_id,session_id,kind,time.time(),dumps(data)))

    def events(self, *, run_id=None, session_id=None, after=0, limit=1000):
        clause, value = ('run_id',run_id) if run_id else ('session_id',session_id)
        with self.connect() as db:
            rows=db.execute(f'SELECT * FROM events WHERE {clause}=? AND seq>? ORDER BY seq LIMIT ?', (value,after,limit)).fetchall()
        return [Event(**{**dict(r),'data':json.loads(r['data'])}) for r in rows]

    def stop(self, id):
        with self.connect() as db:
            db.execute("UPDATE runs SET stop_requested=1, updated=? WHERE id=? AND state IN ('starting','running','stopping')",(time.time(),id))

    def finish(self, id, state, code=None, exit_code=None):
        if state not in TERMINAL:
            raise ValueError(state)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM runs WHERE id=?',(id,)).fetchone()
            if row['state'] in TERMINAL:
                return
            if row['stop_requested'] and state not in ('interrupted',):
                state,code='cancelled','user_stop'
            self._event(db,id,row['session_id'],'run_finished',{'state':state,'code':code,'exit_code':exit_code})
            db.execute('UPDATE runs SET state=?,error=?,exit_code=?,updated=? WHERE id=?',(state,code,exit_code,time.time(),id))
