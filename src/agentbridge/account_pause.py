"""Durable account routing pause; authentication and active turns are retained."""

import time

from .account_probe import stamp
from .errors import BridgeError


def migrate_v8(db, version):
    if version != 7:
        return version
    db.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS paused_accounts(
            account_id TEXT PRIMARY KEY, paused_at REAL NOT NULL);
        UPDATE metadata SET version=8;
        COMMIT;
    ''')
    return 8


class AccountPauseStoreMixin:
    def pause_status(self, account_id):
        with self.connect() as db:
            row = db.execute('SELECT paused_at FROM paused_accounts WHERE account_id=?',
                             (account_id,)).fetchone()
        return {'paused': row is not None,
                'paused_at': stamp(row['paused_at']) if row is not None else None}

    def paused_account_ids(self):
        with self.connect() as db:
            return {row['account_id'] for row in db.execute('SELECT account_id FROM paused_accounts')}

    def set_account_paused(self, account_id, paused):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM accounts WHERE id=?', (account_id,)).fetchone() is None:
                raise BridgeError('account_not_found', 'No account matches that reference.')
            if db.execute('SELECT 1 FROM retired_accounts WHERE account_id=?',
                          (account_id,)).fetchone():
                raise BridgeError('account_retired', 'The selected proxy account was retired.')
            if paused:
                db.execute('INSERT OR IGNORE INTO paused_accounts(account_id,paused_at) VALUES (?,?)',
                           (account_id, time.time()))
            else:
                db.execute('DELETE FROM paused_accounts WHERE account_id=?', (account_id,))
            row = db.execute('SELECT paused_at FROM paused_accounts WHERE account_id=?',
                             (account_id,)).fetchone()
        return {'paused': row is not None,
                'paused_at': stamp(row['paused_at']) if row is not None else None}


class AccountPauseMixin:
    def _set_account_paused(self, account_ref, account_id, paused):
        if (account_ref is None) == (account_id is None):
            raise BridgeError('invalid_request', 'Provide one account_ref or account_id.')
        account = (self.account_service.get(account_id) if account_id is not None else
                   self.account_service.resolve(account_ref))
        state = self.store.set_account_paused(account.id, paused)
        return {'account_id': account.id, 'account_ref': self.account_reference(account.id),
                'routing': state}

    def account_pause(self, account_ref=None, *, account_id=None):
        return self._set_account_paused(account_ref, account_id, True)

    def account_resume(self, account_ref=None, *, account_id=None):
        return self._set_account_paused(account_ref, account_id, False)
