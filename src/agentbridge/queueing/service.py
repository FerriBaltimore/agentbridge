"""Provider-neutral SDK queue operations and input admission."""

from dataclasses import asdict, replace
from hashlib import sha256
import json

from ..errors import BridgeError
from ..execution_context import verify
from ..models import identifier
from ..routing.admission import normalized_exclusions
from ..security import Redactor
from ..store import dumps
from ..transports import command
from ..workspace_policy import validate_execution_workspace
from .connection import referenced_secrets, request
from .delivery import dispatch, validate_steering
from .persistence import QueueStore, public_item


class QueueMixin:
    def queue_list(self, instance_id, *, limit=100, cursor=0):
        return QueueStore(self.store).snapshot(instance_id, limit=limit, cursor=cursor)

    def queue_add(self, instance_id, content, **options):
        if 'delivery' in options:
            raise BridgeError('invalid_request', 'queues.add always uses queue delivery.')
        return self.message_create(instance_id, content, delivery='queue', **options)

    def queue_move(self, instance_id, message_id, position, *, expected_version=None):
        return QueueStore(self.store).move(instance_id, message_id, position,
                                          expected_version=expected_version)

    def queue_delete(self, instance_id, message_id, *, expected_version=None):
        return QueueStore(self.store).delete(instance_id, message_id, expected_version=expected_version)

    def queue_pause(self, instance_id, *, expected_version=None):
        return QueueStore(self.store).pause(instance_id, expected_version=expected_version)

    def queue_resume(self, instance_id, *, expected_version=None, message_id=None,
                     context_package=None, mcp=None):
        from ..execution_context import prepare
        self.get_session(instance_id)
        execution = None
        if message_id is not None:
            row = QueueStore(self.store).get(message_id)
            if row['session_id'] != instance_id:
                raise BridgeError('message_not_found', 'No matching queued message exists.')
            from ..models import RunOptions
            execution, _, _ = prepare(context_package, mcp)
            verify(RunOptions(**json.loads(row['options'])), execution)
        elif context_package is not None or mcp is not None:
            raise BridgeError('message_id_required', 'Bind fresh private context to a message_id.')
        request(self.root, instance_id, {'message_id': message_id, 'execution': execution,
                                        'secrets': referenced_secrets(self)})
        return QueueStore(self.store).resume(instance_id, expected_version=expected_version)

    def queue_dispatch(self, instance_id, message_id, *, mode, expected_version=None,
                       expected_turn_id=None):
        identifier(instance_id)
        identifier(message_id)
        if mode not in ('steer', 'interrupt'):
            raise BridgeError('invalid_delivery', 'Immediate delivery must be steer or interrupt.')
        if mode == 'interrupt':
            request(self.root, instance_id, {'action': 'check', 'message_id': message_id,
                                            'secrets': referenced_secrets(self)})
        dispatch(self.store, instance_id, message_id, mode, expected_version=expected_version,
                 expected_turn_id=expected_turn_id)
        return self.message_get(message_id)

    def message_get(self, message_id):
        identifier(message_id)
        queue = QueueStore(self.store)
        try:
            row = queue.get(message_id)
        except BridgeError as error:
            if error.code != 'message_not_found':
                raise
            with self.store.connect() as db:
                run = db.execute('SELECT id,prompt FROM runs WHERE message_id=?', (message_id,)).fetchone()
            if run is None:
                raise error
            return {**self.turn(run['id']), 'content': run['prompt']}
        value = public_item(row)
        value['queue_state'] = row['state']
        value['account_ref'] = None
        if row['turn_id']:
            turn = self.turn(row['turn_id'])
            value['account_ref'] = turn['account_ref']
            if row['state'] == 'dispatched':
                value['state'] = turn['state']
                value['error'] = turn['error']
        return value

    def _queue_submit(self, instance_id, prompt, options, *, execution, exclusions,
                      idempotency_key, position, expected_version, delivery, expected_turn_id):
        identifier(instance_id)
        exclusions = normalized_exclusions(exclusions)
        raw = {'instance_id': instance_id, 'content': prompt, 'options': asdict(options),
               'exclusions': list(exclusions), 'position': position, 'delivery': delivery,
               'expected_turn_id': expected_turn_id}
        digest = sha256(dumps(raw).encode()).hexdigest()
        queue = QueueStore(self.store)
        previous = queue.replay(idempotency_key, digest)
        if previous:
            return {**self.message_get(previous['id']), 'replayed': True}
        session = self.get_session(instance_id)
        validate_execution_workspace(session['cwd'], self.root,
                                     workspace_write=options.sandbox != 'read-only')
        account = self.account(session['account_id'])
        options = replace(options, model=options.model or session['model'], steerable=True)
        command(account, session, options)
        if delivery in ('steer', 'interrupt'):
            from .records import active
            with self.store.connect() as db:
                run = active(db, instance_id)
                if delivery == 'steer':
                    validate_steering(db, {'session_id': instance_id, 'options': dumps(asdict(options)),
                                          'exclusions': dumps(list(exclusions))}, run)
                if expected_turn_id is not None and (run is None or run['id'] != expected_turn_id):
                    raise BridgeError('turn_conflict', 'The active turn changed before immediate delivery.')
        if self.store.routing(instance_id)['mode'] != 'automatic' and exclusions:
            raise BridgeError('invalid_request', 'Pinned accounts cannot use route exclusions.')
        secrets = referenced_secrets(self)
        capability = execution['mcp']['capability'] if execution and execution.get('mcp') else ''
        redactor = Redactor((*secrets.values(), capability))
        prompt = redactor.clean(prompt)
        options = replace(options, attachments=tuple(
            {key: redactor.clean(value) if key in {'name', 'text'} else value
             for key, value in item.items()} for item in options.attachments))
        message_id, created = queue.add(instance_id, prompt, options, exclusions,
                                        idempotency_key, digest, position=position,
                                        expected_version=expected_version)
        if not created:
            return {**self.message_get(message_id), 'replayed': True}
        try:
            request(self.root, instance_id, {'message_id': message_id, 'execution': execution,
                                            'secrets': secrets, 'activate': delivery == 'queue'})
            if delivery != 'queue':
                dispatch(self.store, instance_id, message_id, delivery,
                         expected_turn_id=expected_turn_id)
        except BridgeError as error:
            # The caller always gets the durable identity, even if the private
            # handoff fails. No second submission is needed to recover this input.
            error.details = {**error.details, 'message_id': message_id, 'instance_id': instance_id}
            raise
        return {**self.message_get(message_id), 'replayed': False}
