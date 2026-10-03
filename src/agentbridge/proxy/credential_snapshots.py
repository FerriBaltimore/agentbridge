"""Trusted host-only credential capture; no remote storage or provider authority claims."""

from datetime import datetime, timezone
from functools import wraps
import json

from ..checkpoint import content, native, state
from ..errors import BridgeError
from ..models import identifier
from . import credential_barrier as barrier
from . import credential_content as capsule
from .managed import ManagedProxyClient


def safe(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as error:
            code = getattr(error, 'code', '')
            allowed = {'credential_snapshot_pending', 'credential_snapshot_corrupt',
                       'credential_snapshot_conflict', 'credential_snapshot_proof_required',
                       'credential_snapshot_scope', 'credential_snapshot_reauthentication',
                       'credential_snapshot_identity', 'credential_snapshot_authority',
                       'credential_snapshot_unsupported'}
            if code not in allowed:
                code = 'credential_snapshot_failed'
            raise BridgeError(code, 'The private credential operation could not complete.',
                              retryable=code == 'credential_snapshot_pending') from None
    return call


def fail(code='credential_snapshot_scope'):
    raise BridgeError(code, 'The private credential operation scope is invalid.')


def accounts(db, account_ids):
    if (not isinstance(account_ids, (list, tuple)) or not account_ids
            or len(set(account_ids)) != len(account_ids) or len(account_ids) > 1000):
        fail()
    for account_id in account_ids:
        identifier(account_id)
        row = db.execute('SELECT config FROM accounts WHERE id=?', (account_id,)).fetchone()
        if row is None or not ManagedProxyClient.is_managed(json.loads(row[0]), account_id):
            fail()


def validate(descriptor, owner_ref):
    fields = {'format_version', 'snapshot_id', 'owner_ref', 'component', 'created_at',
              'barrier_id', 'content', 'members', 'credential_refs', 'restore_authentication'}
    if (not isinstance(descriptor, dict) or set(descriptor) != fields
            or descriptor['format_version'] != '1' or descriptor['owner_ref'] != owner_ref
            or descriptor['component'] != 'cliproxyapi' or descriptor['members'] != ['proxy_auth']
            or descriptor['restore_authentication'] != 'unverified'):
        fail()
    state.canonical_uuid(descriptor['snapshot_id'])
    state.canonical_uuid(descriptor['barrier_id'])
    return {key: value for key, value in descriptor.items() if key != 'content'}


class CredentialSnapshots:
    def __init__(self, store, managed_proxy):
        self.store, self.managed_proxy = store, managed_proxy

    @safe
    def capture_online(self, *, operation_id, account_ids):
        from .credential_online import capture

        return capture(self.store, self.managed_proxy, operation_id, account_ids)

    @safe
    def normalize_login_adapters(self, *, aliases):
        """Normalize only known read-only aliases explicitly supplied by the trusted host."""
        from .credential_configuration import normalize_login_adapters

        return normalize_login_adapters(self.store, aliases)

    @safe
    def configuration(self):
        from .credential_configuration import configuration

        return configuration(self.store)

    @safe
    def verify_current(self, *, snapshot_id, account_ids=None):
        from .credential_online import verify_current

        return verify_current(self.store, self.managed_proxy, snapshot_id, account_ids)

    @safe
    def capture(self, *, operation_id, account_ids, proof_ref, verify_quiescence):
        state.canonical_uuid(operation_id)
        state.canonical_uuid(proof_ref)
        if not callable(verify_quiescence):
            fail('credential_snapshot_proof_required')
        with content.instance_lock(self.store, operation_id):
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                barrier.initialize(db)
                accounts(db, account_ids)
                identity = state.identity(db)
                if not identity['enabled']:
                    fail('credential_snapshot_authority')
                scope = {key: identity[key] for key in state.IDENTITY_KEYS}
                scope.update(account_ids=sorted(account_ids), barrier_id=operation_id)
                payload = json.dumps(scope, sort_keys=True)
                row = db.execute('SELECT * FROM credential_capture_operations '
                                 'WHERE operation_id=?', (operation_id,)).fetchone()
                if row is not None and (row['payload'] != payload or
                                       row['proof_ref'] not in (None, proof_ref)):
                    fail('credential_snapshot_conflict')
                if row is not None and row['descriptor']:
                    descriptor = json.loads(row['descriptor'])
                    if capsule.read(self.store, descriptor['content']) != validate(
                            descriptor, identity['owner_ref']):
                        fail('credential_snapshot_corrupt')
                    return descriptor
                created = row['created_at'] if row else datetime.now(timezone.utc).isoformat()
                db.execute('INSERT OR IGNORE INTO credential_capture_operations '
                           '(operation_id,payload,created_at) VALUES (?,?,?)',
                           (operation_id, payload, created))
                for account_id in account_ids:
                    held = barrier.hold(db, account_id)
                    if held and (held[0] != operation_id or held[1] != 'capture'):
                        fail('credential_snapshot_pending')
                    db.execute('INSERT OR IGNORE INTO credential_account_holds VALUES (?,?,?)',
                               (account_id, operation_id, 'capture'))
            with self.store.connect() as db:
                barrier.require_drained(db, account_ids)
            for account_id in account_ids:
                self.managed_proxy.suspend_for_capture(account_id)
            if verify_quiescence(dict(scope)) is not True:
                fail('credential_snapshot_proof_required')
            with self.store.connect() as db:
                db.execute('UPDATE credential_capture_operations SET proof_ref=? '
                           'WHERE operation_id=?', (proof_ref, operation_id))
            base = {'format_version': '1', 'snapshot_id': operation_id,
                    'owner_ref': identity['owner_ref'], 'component': 'cliproxyapi',
                    'created_at': created, 'barrier_id': operation_id, 'members': ['proxy_auth'],
                    'credential_refs': sorted(account_ids), 'restore_authentication': 'unverified'}
            path = content.object_path(self.store, operation_id)
            if path.exists():
                checksum, size = native.digest(path)
                reference = {'content_id': operation_id, 'sha256': checksum, 'bytes': size}
                if capsule.read(self.store, reference) != base:
                    fail('credential_snapshot_corrupt')
            else:
                reference = capsule.capture(self.store, account_ids, base)
            descriptor = {**base, 'content': reference}
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('UPDATE credential_capture_operations SET descriptor=? '
                           'WHERE operation_id=?', (json.dumps(descriptor), operation_id))
                db.execute('DELETE FROM credential_account_holds WHERE operation_id=? '
                           "AND reason='capture'", (operation_id,))
            return descriptor

    @safe
    def resolve_content(self, reference):
        return content.resolve(self.store, reference)

    @safe
    def register_content(self, reference, source):
        content.register(self.store, reference, source)

    @safe
    def restore(self, descriptor):
        from .credential_restore import restore

        return restore(self.store, descriptor)

    @safe
    def verify_restored(self, account_id, *, proof_ref, verify_authority, verify_credential):
        from .credential_restore import verify

        return verify(self.store, self.managed_proxy, account_id, proof_ref,
                      verify_authority, verify_credential)

    @safe
    def authorize_reconnection(self, account_id, *, proof_ref, verify_authority):
        from .credential_reconnection import authorize

        return authorize(self.store, self.managed_proxy, account_id, proof_ref, verify_authority)
