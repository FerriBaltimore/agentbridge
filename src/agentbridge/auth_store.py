"""Durable authentication attempt records for the optional GrantBridge adapter."""
import json
import time

from .errors import BridgeError, BusyError
from .models import account_name_key


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class AuthStoreMixin:
    def auth_attempt_for_request(self, owner, request_key):
        with self.connect() as db:
            row = db.execute('SELECT * FROM auth_attempts WHERE owner=? AND request_key=?',
                             (owner, request_key)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value['data'] = json.loads(value['data'])
        return value

    def create_auth_attempt(self, attempt, *, request_key=None, connection=None,
                            proxy_route=None):
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
                    route = db.execute('SELECT config FROM auth_proxy_routes WHERE attempt_id=?',
                                       (value['id'],)).fetchone()
                    if proxy_route is not None and (route is None or json.loads(route['config']) != proxy_route):
                        raise BridgeError('idempotency_conflict', 'Request key already belongs to a different proxy route.')
                    value['data'] = json.loads(value['data'])
                    return value, False
            if proxy_route is not None:
                for row in db.execute('''SELECT id,config FROM accounts
                    WHERE id NOT IN (SELECT account_id FROM retired_accounts)'''):
                    existing = json.loads(row['config'])
                    if (row['id'] != attempt['account_id']
                            and existing.get('proxy_base_url') == proxy_route['proxy_base_url']):
                        raise BridgeError('proxy_endpoint_shared',
                                          'The proxy endpoint belongs to another account.')
                retired = db.execute('''SELECT r.config FROM auth_attempts a
                    JOIN auth_proxy_routes r ON r.attempt_id=a.id
                    WHERE a.status='abandoned' ''')
                for row in retired:
                    if json.loads(row['config'])['proxy_base_url'] == proxy_route['proxy_base_url']:
                        raise BridgeError('proxy_endpoint_retired',
                                          'An uncertain OAuth attempt requires a new dedicated local proxy endpoint.')
                active = db.execute('''SELECT a.account_id,a.name,r.config
                    FROM auth_attempts a JOIN auth_proxy_routes r ON r.attempt_id=a.id
                    WHERE a.status NOT IN ('failed','cancelled','abandoned','expired',
                                           'revoked','replaced','bound','usable')''')
                for row in active:
                    other_route = json.loads(row['config'])
                    if (row['account_id'] == attempt['account_id']
                            or account_name_key(row['name']) == account_name_key(attempt['name'])
                            or other_route['proxy_base_url'] == proxy_route['proxy_base_url']):
                        raise BusyError()
                if attempt['engine'] in {'codex', 'claude'}:
                    callback_owner = db.execute('''SELECT 1 FROM auth_attempts
                        WHERE engine=? AND status NOT IN
                        ('failed','cancelled','abandoned','expired','revoked',
                         'replaced','bound','usable') LIMIT 1''',
                        (attempt['engine'],)).fetchone()
                    if callback_owner:
                        raise BridgeError('authentication_in_progress',
                                          'Finish the current provider login before starting another.')
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
            if proxy_route is not None:
                db.execute('INSERT INTO auth_proxy_routes(attempt_id,config,connection) VALUES (?,?,?)',
                           (attempt['id'], dumps(proxy_route), dumps(connection or {})))
        return attempt, True

    def auth_proxy_route(self, attempt_id):
        with self.connect() as db:
            row = db.execute('SELECT config,connection FROM auth_proxy_routes WHERE attempt_id=?',
                             (attempt_id,)).fetchone()
        if row is None:
            raise BridgeError('authentication_route_missing', 'This login has no proxy route.')
        return {'config': json.loads(row['config']), 'connection': json.loads(row['connection'])}

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
            terminal = {'failed', 'cancelled', 'abandoned', 'expired', 'interrupted', 'revoked', 'replaced', 'bound', 'usable'}
            if current['status'] in terminal and not (current['status'] == 'interrupted'
                                                      and status == 'abandoned'):
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
