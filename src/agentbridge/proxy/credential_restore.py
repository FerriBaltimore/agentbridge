"""Materialize credentials into an isolated held Store; regenerate runtime routes privately."""

import json
import os
from pathlib import Path
import shutil
import tempfile

from ..checkpoint import content, native, state
from ..routing.binding import has_bound_proxy_login
from . import credential_barrier as barrier
from . import credential_content as capsule
from .credential_snapshots import accounts, fail, validate
from .management import ManagementClient
from .route import ProxyRoute


def rebind_login_route(db, account_id, previous, route):
    """Move only the authenticated origin's route after proving the restored identity."""
    if not has_bound_proxy_login(db, previous):
        fail('credential_snapshot_identity')
    rows = db.execute("SELECT a.id,a.engine,a.name,a.grantbridge_id,a.data,r.config "
                      "FROM auth_attempts a JOIN auth_proxy_routes r ON r.attempt_id=a.id "
                      "WHERE a.account_id=? AND a.status='bound'", (account_id,)).fetchall()
    for row in rows:
        if (row['engine'] != previous['provider'] or row['name'] != previous['name']
                or not row['grantbridge_id']):
            continue
        try:
            attempt, old_route = json.loads(row['data']), json.loads(row['config'])
        except (TypeError, ValueError):
            continue
        fields = ('proxy_base_url', 'key_env', 'management_key_env')
        if (not isinstance(attempt, dict) or attempt.get('state') != 'usable'
                or not isinstance(old_route, dict)
                or any(old_route.get(key) != previous.get(key) for key in fields)):
            continue
        old_route.update({key: route[key] for key in fields})
        db.execute('UPDATE auth_proxy_routes SET config=? WHERE attempt_id=?',
                   (json.dumps(old_route), row['id']))


def restore(store, descriptor):
    with store.connect() as db:
        expected = state.identity(db)
        base = validate(descriptor, expected['owner_ref'])
        accounts(db, descriptor['credential_refs'])
        if not expected['recovery_held']:
            fail('credential_snapshot_authority')
    operation_id = descriptor['snapshot_id']
    with content.instance_lock(store, operation_id):
        root = native.private_directory(store.root / 'managed-proxies')
        stage = Path(tempfile.mkdtemp(prefix='.restore-auth-', dir=root))
        try:
            if capsule.read(store, descriptor['content'], stage) != base:
                fail('credential_snapshot_corrupt')
            for account_id in descriptor['credential_refs']:
                native.private_directory(stage / 'accounts' / account_id / 'auth')
            content.sync_tree(stage)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                barrier.initialize(db)
                barrier.require_drained(db, descriptor['credential_refs'])
                for account_id in descriptor['credential_refs']:
                    old = db.execute('SELECT * FROM credential_restored_accounts WHERE account_id=?',
                                     (account_id,)).fetchone()
                    if old and (json.loads(old['descriptor']) != descriptor
                                or old['status'] != 'unverified'):
                        fail('credential_snapshot_conflict')
                    held = barrier.hold(db, account_id)
                    if held and held[1] != 'restore_pending' and (
                            held[0] != operation_id or held[1] != 'restore'):
                        fail('credential_snapshot_pending')
                    db.execute('INSERT INTO credential_account_holds VALUES (?,?,?) '
                               'ON CONFLICT(account_id) DO UPDATE SET '
                               'operation_id=excluded.operation_id,reason=excluded.reason',
                               (account_id, operation_id, 'restore'))
                    db.execute('INSERT OR IGNORE INTO credential_restored_accounts '
                               'VALUES (?,?,?,?,NULL)',
                               (account_id, operation_id, json.dumps(descriptor), 'unverified'))
            target_root = native.private_directory(root / 'accounts')
            for account_id in descriptor['credential_refs']:
                target = target_root / account_id
                if target.exists():
                    if target.is_symlink() or set(path.name for path in target.iterdir()) != {'auth'}:
                        fail('credential_snapshot_conflict')
                    if capsule.inventory(root, [account_id]) != capsule.inventory(stage, [account_id]):
                        fail('credential_snapshot_conflict')
                else:
                    os.rename(stage / 'accounts' / account_id, target)
                    content.sync_directory(target_root)
            return {'snapshot_id': operation_id, 'credential_refs': descriptor['credential_refs'],
                    'restore_authentication': 'unverified', 'held': True}
        finally:
            shutil.rmtree(stage)


def verify(store, managed_proxy, account_id, proof_ref, verify_authority, verify_credential):
    state.canonical_uuid(proof_ref)
    if not callable(verify_authority) or not callable(verify_credential):
        fail('credential_snapshot_proof_required')
    with barrier.account_lock(store, account_id):
        with store.connect() as db:
            barrier.initialize(db)
            accounts(db, [account_id])
            row = db.execute('SELECT * FROM credential_restored_accounts WHERE account_id=?',
                             (account_id,)).fetchone()
            if row is None:
                fail()
            if row['status'] == 'verified':
                if row['proof_ref'] != proof_ref:
                    fail('credential_snapshot_conflict')
                return {'account_id': account_id, 'authentication': 'verified', 'held': False}
            held = barrier.hold(db, account_id)
            if held is None or held[0] != row['snapshot_id'] or held[1] != 'restore':
                fail()
            if db.execute('SELECT 1 FROM retired_accounts WHERE account_id=?',
                          (account_id,)).fetchone():
                fail('credential_snapshot_reauthentication')
            barrier.require_drained(db, [account_id])
            identity = state.identity(db)
            scope = {key: identity[key] for key in state.IDENTITY_KEYS}
            scope.update(account_ids=[account_id], barrier_id=row['snapshot_id'])
            config = json.loads(db.execute('SELECT config FROM accounts WHERE id=?',
                                          (account_id,)).fetchone()[0])
            binding = db.execute('SELECT * FROM proxy_bindings WHERE account_id=?',
                                 (account_id,)).fetchone()
            if binding is None:
                fail('credential_snapshot_identity')
            binding = dict(binding)
        if verify_authority(dict(scope)) is not True:
            fail('credential_snapshot_authority')
        route = managed_proxy.provision_for_recovery(account_id)
        try:
            management = ManagementClient(
                ProxyRoute(account_id, route['proxy_base_url'], route['key_env']),
                route['management_key_env'])
            observation = management.observe()
            if (observation['identity_fingerprint'] != binding['identity_fingerprint']
                    or observation['provider'] != config['provider']):
                fail('credential_snapshot_identity')
            verified = verify_credential(dict(scope), dict(route), dict(observation))
            if (not isinstance(verified, dict) or verified.get('authentication') != 'verified'
                    or verified.get('identity_fingerprint') != binding['identity_fingerprint']
                    or observation['status'] != 'active' or observation['disabled'] is not False
                    or observation['unavailable'] is not False or not observation['models']):
                fail('credential_snapshot_reauthentication')
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current_identity = state.identity(db)
                if (tuple(barrier.hold(db, account_id) or ()) != (row['snapshot_id'], 'restore')
                        or any(current_identity[key] != identity[key] for key in state.IDENTITY_KEYS)
                        or json.loads(db.execute('SELECT config FROM accounts WHERE id=?',
                                      (account_id,)).fetchone()[0]) != config):
                    fail('credential_snapshot_conflict')
                if db.execute('SELECT 1 FROM retired_accounts WHERE account_id=?',
                              (account_id,)).fetchone():
                    fail('credential_snapshot_reauthentication')
                rebind_login_route(db, account_id, config, route)
                config.update(route)
                db.execute('UPDATE accounts SET config=? WHERE id=?',
                           (json.dumps(config), account_id))
                db.execute('UPDATE proxy_bindings SET binding_fingerprint=? WHERE account_id=?',
                           (observation['binding_fingerprint'], account_id))
                db.execute("UPDATE credential_restored_accounts SET status='verified',proof_ref=? "
                           'WHERE account_id=?', (proof_ref, account_id))
                db.execute('DELETE FROM credential_account_holds WHERE account_id=?', (account_id,))
            return {'account_id': account_id, 'authentication': 'verified', 'held': False}
        except Exception:
            managed_proxy.suspend_for_capture(account_id)
            raise
