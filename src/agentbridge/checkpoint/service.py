"""Seal one stopped instance under its durable admission fence, without remote I/O."""

from functools import wraps
import json
import shutil
import tempfile
from pathlib import Path

from ..errors import BridgeError
from ..process import alive
from . import content, native, persistence, state


def checkpoint_operation(function):
    @wraps(function)
    def safe_call(*args, **kwargs):
        if kwargs.get('format_version') != '1':
            raise BridgeError('unsupported_version', 'Checkpoint format is unsupported.')
        try:
            return function(*args, **kwargs)
        except Exception as error:
            failure = persistence.safe_error(getattr(error, 'code', None))
            raise BridgeError(failure['code'], 'The checkpoint operation remains unverified.',
                              phase='checkpoint', retryable=failure['retryable']) from None

    return safe_call


def seal(store, turn_id):
    with store.connect() as db:
        attempt = persistence.record(db, turn_id)
        binding = state.identity(db)
    if not attempt:
        native.fail('checkpoint_incomplete')
    with content.instance_lock(store, attempt['instance_id']):
        with store.connect() as db:
            attempt = persistence.record(db, turn_id)
        if attempt['generation'] != binding['store_generation']:
            native.fail('checkpoint_scope_mismatch')
        if attempt['state'] in {'sealed_local', 'ready'}:
            descriptor = json.loads(attempt['descriptor'])
            content.resolve(store, descriptor['content'])
            return descriptor
        if not attempt['process_verified']:
            native.fail('checkpoint_busy')
        run = store.get('runs', turn_id)
        if alive(run['child_pid'], run['child_identity']):
            native.fail('checkpoint_busy')
        session = store.get('sessions', attempt['instance_id'])
        state.canonical_uuid(session['native_id'])
        home = store.root / 'codex-runtime' / session['id']
        native.inventory(home)
        runtime = native.runtime(store, session)
        before = native.fingerprint(home)
        if attempt['source_fingerprint'] and before != attempt['source_fingerprint']:
            native.fail('checkpoint_corrupt')
        with store.connect() as db:
            db.execute('UPDATE native_checkpoints SET source_fingerprint=? WHERE turn_id=?',
                       (before, turn_id))
        stage = Path(tempfile.mkdtemp(prefix='.native-', dir=content.directory(store)))
        try:
            files = native.materialize(home, stage)
            if native.fingerprint(home) != before:
                native.fail('checkpoint_busy')
            payload = content.stage_archive(store, attempt['checkpoint_id'], stage, files)
            with store.connect() as db:
                sealed_at = attempt['sealed_at'] or persistence.now()
                db.execute('UPDATE native_checkpoints SET sealed_at=? WHERE turn_id=?',
                           (sealed_at, turn_id))
            descriptor = {
                'format_version': '1', 'checkpoint_id': attempt['checkpoint_id'],
                **{key: binding[key] for key in state.IDENTITY_KEYS},
                'instance_id': session['id'], 'native_id': session['native_id'],
                'turn_id': turn_id, 'barrier_id': attempt['barrier_id'],
                'execution_cursor': {'format_version': '1',
                                     **{key: binding[key] for key in state.IDENTITY_KEYS},
                                     'seq': attempt['execution_seq']},
                'state': 'sealed', 'sealed_at': sealed_at,
                'runtime': runtime, 'files': files, 'content': payload,
            }
            content.publish_descriptor(store, descriptor)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = persistence.record(db, turn_id)
                if (current['barrier_id'] != attempt['barrier_id']
                        or state.identity(db)['store_generation'] != binding['store_generation']):
                    native.fail('checkpoint_scope_mismatch')
                db.execute("UPDATE native_checkpoints SET state='sealed_local',descriptor=?,"
                           "error_code=NULL WHERE turn_id=?", (json.dumps(descriptor), turn_id))
            return descriptor
        finally:
            shutil.rmtree(stage, ignore_errors=True)


class Checkpoints:
    """Administrative SDK bound to a trusted Bridge root and its persisted owner."""

    def __init__(self, store):
        self.store = store

    def identity(self):
        with self.store.connect() as db:
            value = state.identity(db)
            return {'format_version': '1', **{key: value[key] for key in state.IDENTITY_KEYS},
                    'enabled': bool(value['enabled']), 'recovery_held': bool(value['recovery_held']),
                    'continuity_holds': [dict(row) for row in db.execute(
                        'SELECT instance_id,reason FROM continuity_holds ORDER BY instance_id')],
                    'cursor': state.cursor(db),
                    'replay_start': state.cursor(db, value['source_seq']),
                    'source_cursor': ({**state.cursor(db, value['source_seq']),
                                       'store_generation': value['source_generation']}
                                      if value['source_generation'] else None)}

    @checkpoint_operation
    def create(self, *, format_version, operation_id, params):
        required = {*state.IDENTITY_KEYS, 'instance_id', 'turn_id'}
        if format_version != '1' or not isinstance(params, dict) or set(params) != required:
            native.fail('checkpoint_invalid')
        state.canonical_uuid(operation_id)
        with self.store.connect() as db:
            binding = state.identity(db)
            if not binding['enabled']:
                raise BridgeError('checkpoint_disabled', 'Durable mode is not enabled on this Store.')
            if any(params[key] != binding[key] for key in state.IDENTITY_KEYS):
                native.fail('checkpoint_scope_mismatch')
            attempt = persistence.record(db, params['turn_id'])
            if (not attempt or attempt['instance_id'] != params['instance_id']
                    or attempt['operation_id'] != operation_id):
                native.fail('checkpoint_scope_mismatch')
            run = db.execute('SELECT * FROM runs WHERE id=?', (params['turn_id'],)).fetchone()
            if run['state'] not in {'completed', 'failed', 'cancelled', 'interrupted', 'incomplete'}:
                native.fail('checkpoint_busy')
        try:
            descriptor = seal(self.store, params['turn_id'])
        except Exception as error:
            with self.store.connect() as db:
                db.execute('UPDATE native_checkpoints SET error_code=? WHERE turn_id=?',
                           (persistence.safe_error(getattr(error, 'code', None))['code'],
                            params['turn_id']))
            persistence.finalize_retry(self.store, params['turn_id'])
            raise BridgeError(persistence.safe_error(getattr(error, 'code', None))['code'],
                              'Checkpoint remains pending; historical result is unchanged.') from None
        persistence.finalize_retry(self.store, params['turn_id'])
        return descriptor

    def resolve_content(self, reference):
        return content.resolve(self.store, reference)

    def register_content(self, reference, source):
        return content.register(self.store, reference, source)

    @checkpoint_operation
    def snapshot_store(self, *, format_version, operation_id, params):
        from .snapshot import snapshot_store

        return snapshot_store(self.store, format_version=format_version,
                              operation_id=operation_id, params=params)

    @checkpoint_operation
    def restore(self, *, format_version, operation_id, params):
        from .restore import restore

        return restore(self.store, format_version=format_version,
                       operation_id=operation_id, params=params)

    def release_recovery(self, *, expected_generation, ready_instances=None):
        from .activation import release_recovery

        return release_recovery(self.store, expected_generation=expected_generation,
                                ready_instances=ready_instances)

    def adopt_drained(self, instance_id, *, operation_id, proof_ref, verify_quiescence):
        from .adoption import adopt_drained

        return adopt_drained(self.store, instance_id, operation_id=operation_id,
                             proof_ref=proof_ref, verify_quiescence=verify_quiescence)
