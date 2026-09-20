"""Durable authentication attempt records for the optional GrantBridge adapter."""
import json
import time

from .errors import BridgeError


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class AuthStoreMixin:
    def create_auth_attempt(self, attempt, *, request_key=None, connection=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if request_key:
                old = db.execute('SELECT * FROM auth_attempts WHERE owner=? AND request_key=?',
                                 (attempt['owner'], request_key)).fetchone()
                if old:
                    value = dict(old)
                    same = all(value.get(key) == attempt.get(key)
                               for key in ('engine', 'name', 'email', 'mode', 'browser'))
                    if not same:
                        raise BridgeError('idempotency_conflict', 'Request key already belongs to different authentication input.')
                    value['data'] = json.loads(value['data'])
                    return value, False
            now = time.time()
            db.execute('''INSERT INTO auth_attempts
                (id,owner,account_id,engine,name,email,mode,browser,request_key,
                 grantbridge_id,status,data,created,updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (attempt['id'], attempt['owner'], attempt['account_id'],
                        attempt['engine'], attempt['name'], attempt.get('email'),
                        attempt['mode'], attempt['browser'], request_key,
                        attempt.get('grantbridge_id'), attempt['status'],
                        dumps(attempt.get('data') or {}), now, now))
            if connection is not None:
                db.execute('INSERT INTO auth_runtime(attempt_id,config) VALUES (?,?)',
                           (attempt['id'], dumps(connection)))
        return attempt, True

    def get_auth_attempt(self, attempt_id, owner=None):
        with self.connect() as db:
            row = db.execute('SELECT * FROM auth_attempts WHERE id=?', (attempt_id,)).fetchone()
        if not row or (owner is not None and row['owner'] != owner):
            raise BridgeError('authentication_attempt_not_found', 'Authentication attempt does not exist.')
        value = dict(row)
        value['data'] = json.loads(value['data'])
        return value

    def latest_auth_attempt(self, account_id):
        with self.connect() as db:
            row = db.execute('''SELECT * FROM auth_attempts
                WHERE account_id=? ORDER BY created DESC LIMIT 1''', (account_id,)).fetchone()
        if not row:
            return None
        value = dict(row)
        value['data'] = json.loads(value['data'])
        return value

    def update_auth_attempt(self, attempt_id, owner, *, status=None, data=None,
                            grantbridge_id=None, account_id=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM auth_attempts WHERE id=? AND owner=?',
                                 (attempt_id, owner)).fetchone()
            if not current:
                raise BridgeError('authentication_attempt_not_found', 'Authentication attempt does not exist.')
            terminal = {'failed', 'cancelled', 'expired', 'interrupted', 'revoked', 'replaced', 'bound', 'usable'}
            if current['status'] in terminal:
                value = dict(current)
                value['data'] = json.loads(value['data'])
                return value
            db.execute('''UPDATE auth_attempts
                SET status=?,data=?,grantbridge_id=?,account_id=?,updated=?
                WHERE id=? AND owner=?''',
                       (status if status is not None else current['status'],
                        dumps(data) if data is not None else current['data'],
                        grantbridge_id if grantbridge_id is not None else current['grantbridge_id'],
                        account_id if account_id is not None else current['account_id'],
                        time.time(), attempt_id, owner))
        return self.get_auth_attempt(attempt_id, owner)
