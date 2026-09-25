"""Durable, account-bound observations and earned-reset redemption receipts."""

import json
import time
from uuid import uuid4

from .errors import BridgeError, BusyError


def migrate_v13(db, version):
    if version != 12:
        return version
    db.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS account_reset_observations(
            account_id TEXT PRIMARY KEY,
            observation_ref TEXT NOT NULL UNIQUE,
            binding_fingerprint TEXT NOT NULL,
            identity_fingerprint TEXT NOT NULL,
            generation INTEGER NOT NULL,
            observed_at REAL NOT NULL,
            data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS account_reset_generations(
            account_id TEXT PRIMARY KEY,
            generation INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS account_reset_attempts(
            idempotency_key TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            observation_ref TEXT NOT NULL,
            credit_id TEXT,
            binding_fingerprint TEXT NOT NULL,
            identity_fingerprint TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('pending','done')),
            dispatch_started REAL,
            outcome TEXT,
            windows_reset INTEGER,
            created REAL NOT NULL,
            updated REAL NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS account_reset_one_observation
            ON account_reset_attempts(account_id,observation_ref);
        CREATE UNIQUE INDEX IF NOT EXISTS account_reset_one_pending
            ON account_reset_attempts(account_id) WHERE state='pending';
        UPDATE metadata SET version=13;
        COMMIT;
    ''')
    return 13


def _row(value):
    if value is None:
        return None
    result = dict(value)
    if 'data' in result:
        result['data'] = json.loads(result['data'])
    return result


def _generation(db, account_id):
    row = db.execute('SELECT generation FROM account_reset_generations WHERE account_id=?',
                     (account_id,)).fetchone()
    return row['generation'] if row is not None else 0


def _advance_generation(db, account_id):
    db.execute('INSERT INTO account_reset_generations(account_id,generation) VALUES (?,1) '
               'ON CONFLICT(account_id) DO UPDATE SET generation=generation+1',
               (account_id,))


class AccountResetStoreMixin:
    def reset_generation(self, account_id):
        with self.connect() as db:
            return _generation(db, account_id)

    def _reset_read_current(self, db, account_id, generation):
        return (_generation(db, account_id) == generation and
                db.execute("SELECT 1 FROM account_reset_attempts WHERE account_id=? "
                           "AND state='pending'", (account_id,)).fetchone() is None)

    def reset_credit_state(self, account_id):
        """Read credit, attempt and generation from one SQLite snapshot."""
        with self.connect() as db:
            db.execute('BEGIN')
            observation = db.execute('SELECT * FROM account_reset_observations WHERE account_id=?',
                                     (account_id,)).fetchone()
            pending = db.execute("SELECT * FROM account_reset_attempts WHERE account_id=? "
                                 "AND state='pending'", (account_id,)).fetchone()
            generation = _generation(db, account_id)
        return {'observation': _row(observation), 'pending': _row(pending),
                'generation': generation}

    def pending_reset_account_ids(self):
        with self.connect() as db:
            return {row['account_id'] for row in db.execute(
                "SELECT account_id FROM account_reset_attempts WHERE state='pending'")}

    def reset_invalidation_at(self, account_id):
        with self.connect() as db:
            row = db.execute("SELECT MAX(updated) AS at FROM account_reset_attempts "
                             "WHERE account_id=? AND state='done' "
                             "AND outcome IN ('reset','already_redeemed')",
                             (account_id,)).fetchone()
        return row['at'] if row is not None else None

    def reset_observation(self, account_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM account_reset_observations WHERE account_id=?',
                             (account_id,)).fetchone()
        return _row(row)

    def expire_reset_observation(self, account_id, expected_generation, observation_ref):
        """A failed refresh cannot leave the previous credit read redeemable."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self._reset_read_current(db, account_id, expected_generation):
                return False
            if observation_ref is None:
                return False
            changed = db.execute('UPDATE account_reset_observations SET observed_at=0 '
                                 'WHERE account_id=? AND observation_ref=? AND generation=?',
                                 (account_id, observation_ref, expected_generation))
            return changed.rowcount == 1

    def save_reset_observation(self, account_id, binding, data, expected_generation):
        """Store only bounded normalized credit facts after verifying the login binding."""
        observed_at = time.time()
        observation_ref = uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self._reset_read_current(db, account_id, expected_generation):
                return None
            if db.execute('SELECT 1 FROM retired_accounts WHERE account_id=?',
                          (account_id,)).fetchone():
                raise BridgeError('account_retired', 'The selected proxy account was retired.')
            current = db.execute('SELECT binding_fingerprint,identity_fingerprint '
                                 'FROM proxy_bindings WHERE account_id=?', (account_id,)).fetchone()
            if current is None or any(current[key] != binding[key] for key in current.keys()):
                raise BridgeError('proxy_binding_changed', 'The proxy account binding changed.')
            db.execute('INSERT INTO account_reset_observations('
                       'account_id,observation_ref,binding_fingerprint,identity_fingerprint,generation,observed_at,data) '
                       'VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET '
                       'observation_ref=excluded.observation_ref,'
                       'binding_fingerprint=excluded.binding_fingerprint,'
                       'identity_fingerprint=excluded.identity_fingerprint,'
                       'generation=excluded.generation,observed_at=excluded.observed_at,'
                       'data=excluded.data',
                       (account_id, observation_ref, binding['binding_fingerprint'],
                        binding['identity_fingerprint'], expected_generation, observed_at,
                        json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':'))))
        return self.reset_observation(account_id)

    def pending_reset_attempt(self, account_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM account_reset_attempts WHERE account_id=? AND state='pending'",
                             (account_id,)).fetchone()
        return _row(row)

    def begin_reset_attempt(self, account_id, idempotency_key, observation_ref, credit_id, binding,
                            *, maximum_age=60):
        """Persist the exact logical attempt before any provider mutation."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM account_reset_attempts WHERE idempotency_key=?',
                                  (idempotency_key,)).fetchone()
            if existing is not None:
                if (existing['account_id'] != account_id or existing['observation_ref'] != observation_ref
                        or existing['credit_id'] != credit_id):
                    raise BridgeError('idempotency_conflict', 'The reset key belongs to another request.')
                if any(existing[key] != binding[key] for key in
                       ('binding_fingerprint', 'identity_fingerprint')):
                    if existing['state'] == 'pending':
                        raise BridgeError('reset_outcome_unknown',
                                          'The pending reset account binding changed.',
                                          phase='redemption', outcome='unknown')
                    raise BridgeError('proxy_binding_changed', 'The proxy account binding changed.')
                if existing['state'] == 'done':
                    return {**_row(existing), 'started_now': False}
                now = time.time()
                dispatched = existing['dispatch_started']
                if dispatched is not None and 0 <= now - dispatched < 90:
                    raise BridgeError('reset_in_progress',
                                      'The saved reset request is still in progress.',
                                      phase='redemption', outcome='unknown',
                                      retry_after_ms=int((90 - (now - dispatched)) * 1000))
                db.execute('UPDATE account_reset_attempts SET dispatch_started=?,updated=? '
                           "WHERE idempotency_key=? AND state='pending'",
                           (now, now, idempotency_key))
                return {**_row(existing), 'dispatch_started': now, 'started_now': False}
            if db.execute('SELECT 1 FROM retired_accounts WHERE account_id=?',
                          (account_id,)).fetchone():
                raise BridgeError('account_retired', 'The selected proxy account was retired.')
            current = db.execute('SELECT binding_fingerprint,identity_fingerprint '
                                 'FROM proxy_bindings WHERE account_id=?', (account_id,)).fetchone()
            if current is None or any(current[key] != binding[key] for key in current.keys()):
                raise BridgeError('proxy_binding_changed', 'The proxy account binding changed.')
            if db.execute("SELECT 1 FROM runs WHERE account_id=? AND state IN ('starting','running','stopping')",
                          (account_id,)).fetchone():
                raise BusyError()
            if db.execute("SELECT 1 FROM auth_attempts WHERE account_id=? AND status NOT IN "
                          "('failed','cancelled','abandoned','expired','revoked','replaced',"
                          "'bound','usable')", (account_id,)).fetchone():
                raise BridgeError('authentication_in_progress',
                                  'Finish the account login before redeeming a reset credit.')
            if db.execute("SELECT 1 FROM account_reset_attempts WHERE account_id=? AND state='pending'",
                          (account_id,)).fetchone():
                raise BridgeError('reset_pending', 'Resolve the pending reset attempt before starting another.')
            observed = db.execute('SELECT * FROM account_reset_observations WHERE account_id=?',
                                  (account_id,)).fetchone()
            if observed is None or observed['observation_ref'] != observation_ref:
                raise BridgeError('reset_observation_changed', 'Refresh available reset credits before redeeming.')
            if observed['generation'] != _generation(db, account_id):
                raise BridgeError('reset_observation_changed', 'Refresh available reset credits before redeeming.')
            age = time.time() - observed['observed_at']
            if not 0 <= age < maximum_age:
                raise BridgeError('reset_observation_stale', 'Refresh available reset credits before redeeming.')
            if any(observed[key] != binding[key] for key in
                   ('binding_fingerprint', 'identity_fingerprint')):
                raise BridgeError('proxy_binding_changed', 'The proxy account binding changed.')
            credit = json.loads(observed['data'])
            if credit.get('status') != 'available' or type(credit.get('available_count')) is not int \
                    or credit['available_count'] <= 0:
                raise BridgeError('reset_credit_unavailable', 'No earned reset credit is verified as available.')
            details = credit.get('credits')
            if credit_id is not None and (not isinstance(details, list) or not any(
                    item.get('id') == credit_id and item.get('status') == 'available'
                    for item in details)):
                raise BridgeError('reset_credit_changed', 'Refresh the selected reset credit before redeeming.')
            if db.execute('SELECT 1 FROM account_reset_attempts WHERE account_id=? '
                          'AND observation_ref=?', (account_id, observation_ref)).fetchone():
                raise BridgeError('reset_observation_used', 'Refresh reset credits before another redemption.')
            now = time.time()
            db.execute('INSERT INTO account_reset_attempts('
                       'idempotency_key,account_id,observation_ref,credit_id,binding_fingerprint,'
                       'identity_fingerprint,state,dispatch_started,created,updated) '
                       'VALUES (?,?,?,?,?,?,\'pending\',?,?,?)',
                       (idempotency_key, account_id, observation_ref, credit_id,
                        binding['binding_fingerprint'], binding['identity_fingerprint'], now, now, now))
            _advance_generation(db, account_id)
            row = db.execute('SELECT * FROM account_reset_attempts WHERE idempotency_key=?',
                             (idempotency_key,)).fetchone()
        return {**_row(row), 'started_now': True}

    def release_reset_attempt(self, idempotency_key, dispatch_started):
        """Allow an explicit retry after this dispatch returned an unknown result."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE account_reset_attempts SET dispatch_started=NULL,updated=? "
                       "WHERE idempotency_key=? AND state='pending' AND dispatch_started=?",
                       (time.time(), idempotency_key, dispatch_started))

    def finish_reset_attempt(self, idempotency_key, outcome, windows_reset,
                             *, dispatch_started=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM account_reset_attempts WHERE idempotency_key=?',
                             (idempotency_key,)).fetchone()
            if row is None:
                raise BridgeError('reset_attempt_missing', 'The reset attempt is unavailable.')
            if row['state'] == 'done':
                return _row(row)
            if outcome == 'not_started' and row['dispatch_started'] != dispatch_started:
                raise BridgeError('reset_outcome_unknown',
                                  'Another dispatch may have reached Codex.',
                                  phase='redemption', outcome='unknown')
            db.execute("UPDATE account_reset_attempts SET state='done',dispatch_started=NULL,"
                       "outcome=?,windows_reset=?,updated=? WHERE idempotency_key=?",
                       (outcome, windows_reset, time.time(), idempotency_key))
            _advance_generation(db, row['account_id'])
            done = db.execute('SELECT * FROM account_reset_attempts WHERE idempotency_key=?',
                              (idempotency_key,)).fetchone()
        return _row(done)
