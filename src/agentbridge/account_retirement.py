"""Durable local route retirement and confirmed proxy stop evidence."""

import json
import time

from .errors import BridgeError, BusyError


def migrate_v7(db, version):
    """A tombstone fences routing; confirmation of proxy stop is separate."""
    if version != 6:
        return version
    db.execute('BEGIN IMMEDIATE')
    columns = {row['name'] for row in db.execute('PRAGMA table_info(retired_accounts)')}
    if 'proxy_retired_at' not in columns:
        db.execute('ALTER TABLE retired_accounts ADD COLUMN proxy_retired_at REAL')
    db.execute('UPDATE metadata SET version=7')
    return 7


class AccountRetirementStoreMixin:
    def retired_account_ids(self):
        with self.connect() as db:
            return {row['account_id'] for row in db.execute('SELECT account_id FROM retired_accounts')}

    def retirement_status(self, account_id):
        """Observe a durable stop attestation without contacting the sidecar."""
        with self.connect() as db:
            row = db.execute('SELECT proxy_retired_at FROM retired_accounts WHERE account_id=?',
                             (account_id,)).fetchone()
        return {'retired': row is not None,
                'local_proxy_stopped': row is not None and row['proxy_retired_at'] is not None}

    def retire_account(self, account_id):
        """Fence a route before requesting sidecar stop; retain historical evidence."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT config FROM accounts WHERE id=?', (account_id,)).fetchone()
            if row is None:
                raise BridgeError('account_not_found', 'No account matches that reference.')
            account = json.loads(row['config'])
            reference = account.get('name') or account_id
            existing = db.execute('SELECT 1 FROM retired_accounts WHERE account_id=?',
                                  (account_id,)).fetchone()
            if existing is None:
                active = db.execute("SELECT 1 FROM runs WHERE account_id=? AND state IN "
                                    "('starting','running','stopping')", (account_id,)).fetchone()
                if active:
                    raise BusyError()
                reset_pending = db.execute(
                    "SELECT 1 FROM account_reset_attempts WHERE account_id=? AND state='pending'",
                    (account_id,)).fetchone()
                if reset_pending:
                    raise BridgeError('reset_pending',
                                      'Resolve the pending reset attempt before removing this account.')
                pending = db.execute("SELECT 1 FROM auth_attempts WHERE account_id=? AND status NOT IN "
                    "('failed','cancelled','expired','revoked','replaced','bound','usable')",
                    (account_id,)).fetchone()
                if pending:
                    raise BridgeError('authentication_in_progress',
                                      'Cancel or complete the pending login before removing this account.')
                db.execute('INSERT INTO retired_accounts(account_id,retired_at) VALUES (?,?)',
                           (account_id, time.time()))
                db.execute('DELETE FROM proxy_bindings WHERE account_id=?', (account_id,))
                db.execute('DELETE FROM auth_proxy_routes WHERE attempt_id IN '
                           '(SELECT id FROM auth_attempts WHERE account_id=?)', (account_id,))
        return {'account_ref': reference, 'account_id': account_id, 'removed': True,
                'upstream_credential_removed': False}

    def attest_proxy_retirement(self, account_id):
        """Persist the supervisor's confirmed stop before reporting success."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            updated = db.execute('UPDATE retired_accounts SET proxy_retired_at=COALESCE('
                                 'proxy_retired_at, ?) WHERE account_id=?',
                                 (time.time(), account_id)).rowcount
            if updated != 1:
                raise BridgeError('account_not_found', 'No retired account matches that reference.')


class AccountRetirementMixin:
    def account_delete(self, account_ref=None, *, account_id=None):
        if (account_ref is None) == (account_id is None):
            raise BridgeError('invalid_request', 'Provide one account_ref or account_id.')
        account = (self.account_service.get(account_id) if account_id is not None else
                   self.account_service.resolve(account_ref, include_retired=True))
        result = self.store.retire_account(account.id)
        if not self.managed_proxy.is_managed(account.to_dict(), account.id):
            # The owner of an external proxy must stop it. Only AgentBridge's
            # managed sidecar can receive a local stop attestation.
            return result
        try:
            if self.store.retirement_status(account.id)['local_proxy_stopped']:
                return result
            stopped = self.managed_proxy.retire(account.id)
            if stopped != {'retired': True, 'upstream_credential_removed': False}:
                raise ValueError('Proxy stop has no confirmed result')
            self.store.attest_proxy_retirement(account.id)
        except Exception:
            # The tombstone is already durable. A response cannot claim the proxy stopped.
            raise BridgeError('unknown_outcome',
                              'The local proxy retirement could not be confirmed.',
                              phase='execution', outcome='unknown') from None
        return result
