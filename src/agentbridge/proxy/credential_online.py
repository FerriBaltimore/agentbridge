"""Coherent credential writer exports while unrelated and same-account execution continues."""

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from ..checkpoint import content, native, state
from . import credential_barrier as barrier
from . import credential_content as capsule
from .credential_snapshots import accounts, fail, validate
from .credential_writer import export_path
from .credential_writer_content import read_export, revision


def context(store, account_ids):
    """Account/login authority and bindings are checked independently of mutable auth bytes."""
    with store.connect() as db:
        accounts(db, account_ids)
        identity = state.identity(db)
        if not identity['enabled'] or identity['recovery_held']:
            fail('credential_snapshot_authority')
        result = {key: identity[key] for key in state.IDENTITY_KEYS}
        bindings = {}
        for account_id in sorted(account_ids):
            if barrier.hold(db, account_id) or db.execute(
                    'SELECT 1 FROM retired_accounts WHERE account_id=?', (account_id,)).fetchone():
                fail('credential_snapshot_pending')
            if db.execute("SELECT 1 FROM auth_attempts WHERE account_id=? AND status NOT IN "
                          "('failed','cancelled','expired','interrupted','abandoned','revoked',"
                          "'replaced','bound','usable')", (account_id,)).fetchone():
                fail('credential_snapshot_pending')
            config = json.loads(db.execute('SELECT config FROM accounts WHERE id=?',
                                           (account_id,)).fetchone()[0])
            binding = db.execute('SELECT * FROM proxy_bindings WHERE account_id=?',
                                 (account_id,)).fetchone()
            if binding is None:
                fail('credential_snapshot_identity')
            encoded = json.dumps({'config': config, 'binding': dict(binding)}, sort_keys=True)
            bindings[account_id] = hashlib.sha256(encoded.encode()).hexdigest()
        return {**result, 'accounts': bindings}


def current(store, managed, evidence):
    if (not isinstance(evidence, dict) or set(evidence) != {'context', 'revisions'}
            or not isinstance(evidence['context'], dict)
            or not isinstance(evidence['revisions'], dict)):
        fail('credential_snapshot_pending')
    account_ids = sorted(evidence['revisions'])
    if context(store, account_ids) != evidence['context']:
        fail('credential_snapshot_pending')
    for account_id, expected in evidence['revisions'].items():
        if revision(managed.credential_revision(account_id).get('revision')) != revision(expected):
            fail('credential_snapshot_pending')
    if context(store, account_ids) != evidence['context']:
        fail('credential_snapshot_pending')


def capture(store, managed, operation_id, account_ids):
    state.canonical_uuid(operation_id)
    with ExitStack() as locks:
        locks.enter_context(content.instance_lock(store, operation_id))
        initial = context(store, account_ids)
        for account_id in sorted(account_ids):
            locks.enter_context(barrier.account_lock(store, account_id))
        payload = json.dumps({'mode': 'online', 'context': initial}, sort_keys=True)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            barrier.initialize(db)
            row = db.execute('SELECT * FROM credential_capture_operations WHERE operation_id=?',
                             (operation_id,)).fetchone()
            if row and row['payload'] != payload:
                fail('credential_snapshot_conflict')
            if row and row['descriptor']:
                descriptor = json.loads(row['descriptor'])
                if capsule.read(store, descriptor['content']) != validate(
                        descriptor, initial['owner_ref']):
                    fail('credential_snapshot_corrupt')
                return descriptor
            created = row['created_at'] if row else datetime.now(timezone.utc).isoformat()
            db.execute('INSERT OR IGNORE INTO credential_capture_operations '
                       '(operation_id,payload,created_at) VALUES (?,?,?)',
                       (operation_id, payload, created))
        base = {'format_version': '1', 'snapshot_id': operation_id,
                'owner_ref': initial['owner_ref'], 'component': 'cliproxyapi',
                'created_at': created, 'barrier_id': operation_id, 'members': ['proxy_auth'],
                'credential_refs': sorted(account_ids), 'restore_authentication': 'unverified'}
        path = content.object_path(store, operation_id)
        if path.exists():
            checksum, size = native.digest(path)
            reference = {'content_id': operation_id, 'sha256': checksum, 'bytes': size}
            if (capsule.read(store, reference) != base
                    or capsule.writer_state(store, reference).get('context') != initial):
                fail('credential_snapshot_corrupt')
        else:
            reference = materialize(store, managed, base, initial)
        descriptor = {**base, 'content': reference}
        # A refresh after materialization does not change this historical coherent seal.
        # verify_current separately decides whether it can represent current protection.
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if {key: state.identity(db)[key] for key in state.IDENTITY_KEYS} != {
                    key: initial[key] for key in state.IDENTITY_KEYS}:
                fail('credential_snapshot_scope')
            db.execute('UPDATE credential_capture_operations SET descriptor=? '
                       'WHERE operation_id=? AND payload=?',
                       (json.dumps(descriptor), operation_id, payload))
        return descriptor


def materialize(store, managed, base, initial):
    stage = Path(tempfile.mkdtemp(prefix='.proxy-online-', dir=content.directory(store)))
    capture_id, exports = str(uuid4()), []
    files, revisions = [], {}
    try:
        for account_id in base['credential_refs']:
            reply = managed.snapshot_files(account_id, capture_id)
            path = export_path(store.root / 'managed-proxies', account_id, capture_id)
            exports.append(path)
            if (not isinstance(reply, dict)
                    or set(reply) != {'capture_id', 'revision', 'sha256', 'bytes'}
                    or reply['capture_id'] != capture_id
                    or native.digest(path) != (reply['sha256'], reply['bytes'])):
                fail('credential_snapshot_corrupt')
            actual, records = read_export(path, destination=stage, account_id=account_id)
            if actual != reply['revision']:
                fail('credential_snapshot_corrupt')
            revisions[account_id] = actual
            files.extend(records)
        evidence = {'context': initial, 'revisions': revisions}
        current(store, managed, evidence)
        return capsule.publish_files(store, stage, base, sorted(files, key=lambda row: row['path']),
                                     writer_state=evidence)
    finally:
        shutil.rmtree(stage)
        for path in exports:
            path.unlink(missing_ok=True)
        root = store.root / 'managed-proxies' / 'captures' / capture_id
        if root.exists() and not root.is_symlink():
            root.rmdir()


def verify_current(store, managed, snapshot_id, account_ids=None):
    state.canonical_uuid(snapshot_id)
    with store.connect() as db:
        row = db.execute('SELECT descriptor FROM credential_capture_operations WHERE operation_id=?',
                         (snapshot_id,)).fetchone()
        if row is None or not row['descriptor']:
            fail('credential_snapshot_pending')
        descriptor = json.loads(row['descriptor'])
        identity = state.identity(db)
    validate(descriptor, identity['owner_ref'])
    if account_ids is not None and sorted(account_ids) != descriptor['credential_refs']:
        fail('credential_snapshot_pending')
    current(store, managed, capsule.writer_state(store, descriptor['content']))
    return {'format_version': '1', 'snapshot_id': snapshot_id, 'current': True}
