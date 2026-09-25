"""Explicit immediate delivery, fenced to the observed active execution."""

import json
import time

from ..errors import BridgeError
from ..models import Account, RunOptions
from ..transports import duplex
from .records import (PENDING, active, changed, item_record, pending, queue_record,
                      reorder, set_paused)


def validate_steering(db, row, run):
    if run is None or run['stop_requested']:
        raise BridgeError('turn_not_active', 'Steering requires an active turn that is not stopping.')
    options = RunOptions(**json.loads(run['options']))
    account = db.execute('SELECT config FROM accounts WHERE id=?', (run['account_id'],)).fetchone()
    if not duplex(Account(**json.loads(account['config'])), options):
        raise BridgeError('steering_unsupported', 'This turn does not accept live input; use interrupt delivery.')
    queued = json.loads(row['options'])
    if queued.get('context_package_digest') or queued.get('mcp_binding_digest'):
        raise BridgeError('steering_context_unsupported', 'Live input cannot replace the active execution context.')
    session = db.execute('SELECT model FROM sessions WHERE id=?', (row['session_id'],)).fetchone()
    current_model = options.model or session['model']
    for field, current in (('model', current_model), ('effort', options.effort),
                           ('sandbox', options.sandbox), ('permission_mode', options.permission_mode),
                           ('context_window', options.context_window)):
        if queued.get(field) is not None and queued[field] != current:
            raise BridgeError('steering_options_conflict', 'Live input must use the active turn settings.')
    if json.loads(row['exclusions']):
        raise BridgeError('steering_options_conflict', 'Live input cannot change account exclusions.')


def dispatch(store, instance_id, message_id, mode, *, expected_version=None, expected_turn_id=None):
    if mode not in ('steer', 'interrupt'):
        raise BridgeError('invalid_delivery', 'Immediate delivery must be steer or interrupt.')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = item_record(db, instance_id, message_id)
        if row['delivery'] == mode and row['state'] not in ('cancelled', 'blocked'):
            if expected_turn_id is not None and row['target_turn_id'] != expected_turn_id:
                raise BridgeError('turn_conflict', 'Immediate delivery belongs to another turn.')
            return
        queue_record(db, instance_id, expected_version)
        if row['state'] not in PENDING or row['delivery'] != 'queue':
            raise BridgeError('message_not_pending', 'This message is no longer available for dispatch.')
        run = active(db, instance_id)
        target = run['id'] if run else None
        if expected_turn_id is not None and target != expected_turn_id:
            raise BridgeError('turn_conflict', 'The active turn changed before immediate delivery.')
        if mode == 'steer':
            validate_steering(db, row, run)
            db.execute("UPDATE queued_messages SET state='steering',delivery='steer',"
                       "target_turn_id=?,turn_id=?,updated=?,error=NULL WHERE id=?",
                       (target, target, time.time(), message_id))
            reorder(db, [item['id'] for item in pending(db, instance_id)])
            changed(store, db, instance_id, 'steering', message_id, target)
        else:
            other = db.execute("SELECT 1 FROM queued_messages WHERE session_id=? AND delivery='interrupt' "
                               "AND state IN ('staged','queued','blocked') AND id<>?",
                               (instance_id, message_id)).fetchone()
            if other:
                raise BridgeError('interrupt_pending', 'Another immediate replacement is already pending.')
            ids = [message_id, *[item['id'] for item in pending(db, instance_id) if item['id'] != message_id]]
            reorder(db, ids)
            db.execute("UPDATE queued_messages SET state='queued',delivery='interrupt',"
                       "target_turn_id=?,updated=?,error=NULL WHERE id=?",
                       (target, time.time(), message_id))
            if run:
                db.execute('UPDATE runs SET stop_requested=1,updated=? WHERE id=?', (time.time(), target))
            set_paused(store, db, instance_id, False)
            changed(store, db, instance_id, 'interrupt_requested', message_id, target)
