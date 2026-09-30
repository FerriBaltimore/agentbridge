"""Host-authorized recovery login; normal execution stays fenced until identity binding."""

import json
import os
import time

from ..checkpoint import content, native, state
from . import credential_barrier as barrier
from . import credential_content as capsule
from .credential_snapshots import accounts, fail


def ensure_login_proxy(store, managed_proxy, account_id, base_url):
    with store.connect() as db:
        held = barrier.hold(db, account_id)
        barrier.require_account(db, account_id, login=True)
    if held:
        return managed_proxy.reconnect_route(account_id, base_url)
    return managed_proxy.ensure(account_id, base_url)


def result(account_id, *, verified=False):
    return {'account_id': account_id, 'authentication': 'verified' if verified else
            'reauthorization_required', 'held': not verified, 'login_allowed': not verified}


def quarantine(store, account_id, generation, proof_ref):
    root = native.private_directory(store.root / 'managed-proxies')
    home = native.private_directory(root / 'accounts' / account_id)
    auth = home / 'auth'
    destination = native.private_directory(
        root / 'quarantine' / generation / account_id / proof_ref) / 'auth'
    if destination.exists():
        # A retry after rename must never move a newly acquired credential into the old capsule.
        if destination.is_symlink() or not destination.is_dir() or any(auth.iterdir()):
            fail('credential_snapshot_conflict')
    elif auth.exists():
        capsule.inventory(root, [account_id])
        content.sync_tree(auth)
        os.rename(auth, destination)
        content.sync_directory(destination.parent)
        content.sync_directory(home)
    native.private_directory(auth)
    route = home / 'route.json'
    if route.is_symlink():
        fail('credential_snapshot_corrupt')
    route.unlink(missing_ok=True)
    content.sync_directory(home)


def authorize(store, managed_proxy, account_id, proof_ref, verify_authority):
    state.canonical_uuid(proof_ref)
    if not callable(verify_authority):
        fail('credential_snapshot_proof_required')
    with barrier.account_lock(store, account_id):
        with store.connect() as db:
            barrier.initialize(db)
            accounts(db, [account_id])
            identity = state.identity(db)
            generation = identity['store_generation']
            held = barrier.hold(db, account_id)
            prior = db.execute('SELECT * FROM credential_reconnections WHERE proof_ref=?',
                               (proof_ref,)).fetchone()
            if db.execute('SELECT 1 FROM retired_accounts WHERE account_id=?',
                          (account_id,)).fetchone():
                fail('credential_snapshot_reauthentication')
            if prior:
                if prior['account_id'] != account_id or prior['generation'] != generation:
                    fail('credential_snapshot_conflict')
                if prior['status'] == 'bound' and held is None:
                    return result(account_id, verified=True)
                if held is None or held[0] != proof_ref:
                    fail('credential_snapshot_conflict')
                if prior['status'] == 'ready' and held[1] == 'reconnect':
                    return result(account_id)
                if prior['status'] != 'preparing' or held[1] != 'reconnect_preparing':
                    fail('credential_snapshot_conflict')
            if held is None or held[1] not in {
                    'restore_pending', 'restore', 'reconnect_preparing', 'reconnect'}:
                fail('credential_snapshot_authority')
            if held[1] == 'reconnect_preparing' and held[0] != proof_ref:
                fail('credential_snapshot_conflict')
            barrier.require_drained(db, [account_id])
            config = json.loads(db.execute('SELECT config FROM accounts WHERE id=?',
                                          (account_id,)).fetchone()[0])
            if not db.execute('SELECT 1 FROM proxy_bindings WHERE account_id=?',
                              (account_id,)).fetchone():
                fail('credential_snapshot_identity')
            scope = {key: identity[key] for key in state.IDENTITY_KEYS}
            scope.update(account_ids=[account_id], barrier_id=proof_ref)
        if verify_authority(dict(scope)) is not True:
            fail('credential_snapshot_authority')
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            barrier.require_drained(db, [account_id])
            db.execute("UPDATE credential_reconnections SET status='superseded' "
                       "WHERE account_id=? AND generation=? AND status='ready'",
                       (account_id, generation))
            db.execute('INSERT OR IGNORE INTO credential_reconnections VALUES (?,?,?,?,?)',
                       (proof_ref, account_id, generation, 'preparing', time.time()))
            db.execute('UPDATE credential_account_holds SET operation_id=?,reason=? '
                       'WHERE account_id=?', (proof_ref, 'reconnect_preparing', account_id))
        managed_proxy.suspend_for_capture(account_id)
        quarantine(store, account_id, generation, proof_ref)
        route = managed_proxy.reconnect_route(account_id)
        if route['proxy_base_url'] == config['proxy_base_url']:
            managed_proxy.suspend_for_capture(account_id)
            fail('credential_snapshot_conflict')
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = barrier.hold(db, account_id)
            if current is None or current[0] != proof_ref or current[1] != 'reconnect_preparing':
                fail('credential_snapshot_conflict')
            config.update(route)
            db.execute('UPDATE accounts SET config=? WHERE id=?', (json.dumps(config), account_id))
            db.execute("UPDATE credential_reconnections SET status='ready' WHERE proof_ref=?",
                       (proof_ref,))
            db.execute("UPDATE credential_restored_accounts SET status='reconnecting',proof_ref=? "
                       'WHERE account_id=?', (proof_ref, account_id))
            db.execute("UPDATE credential_account_holds SET reason='reconnect' WHERE account_id=?",
                       (account_id,))
        return result(account_id)


def finish_reconnection(db, account_id, attempt_id):
    """Called only inside the transaction that has verified and bound the historical identity."""
    held = barrier.hold(db, account_id)
    if held is None:
        return
    if held[1] != 'reconnect':
        fail('credential_snapshot_pending')
    proof = db.execute('SELECT * FROM credential_reconnections WHERE proof_ref=?',
                       (held[0],)).fetchone()
    attempt = db.execute('SELECT created FROM auth_attempts WHERE id=? AND account_id=?',
                         (attempt_id, account_id)).fetchone()
    if (proof is None or proof['account_id'] != account_id or proof['status'] != 'ready'
            or proof['generation'] != state.identity(db)['store_generation'] or attempt is None
            or attempt['created'] < proof['authorized_at']):
        fail('credential_snapshot_authority')
    db.execute("UPDATE credential_reconnections SET status='bound' WHERE proof_ref=?", (held[0],))
    db.execute("UPDATE credential_restored_accounts SET status='verified',proof_ref=? "
               'WHERE account_id=?', (held[0], account_id))
    db.execute('DELETE FROM credential_account_holds WHERE account_id=?', (account_id,))
