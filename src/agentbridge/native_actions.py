"""Explicit native interruption and read-only lookup of admitted user input."""

from .errors import BridgeError
from .models import identifier
from .native_control import request
from .queueing.records import active, set_paused


def interrupt(bridge, turn_id):
    """Pause this conversation's queue, then interrupt its exact live native turn."""
    identifier(turn_id)
    with bridge.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM runs WHERE id=?', (turn_id,)).fetchone()
        if row is None:
            raise BridgeError('not_found', 'Turn does not exist.')
        run = dict(row)
        current = active(db, run['session_id'])
        if current is None or current['id'] != turn_id:
            raise BridgeError('turn_not_active', 'The selected turn is no longer active.')
        set_paused(bridge.store, db, run['session_id'], True, 'native_interrupt')
    result = request(bridge.store, run, 'interrupt')
    return {**result, 'instance_id': run['session_id'], 'turn_id': turn_id}


def lookup(bridge, instance_id, idempotency_key):
    """An absent receipt is not permission to repeat an ambiguous external action."""
    bridge.get_session(instance_id)
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
        raise BridgeError('invalid_request', 'idempotency_key must contain 1-256 characters.')
    with bridge.store.connect() as db:
        row = db.execute('SELECT id AS message_id,session_id FROM queued_messages '
                         'WHERE request_key=?', (idempotency_key,)).fetchone()
        if row is None:
            row = db.execute('SELECT message_id,session_id FROM runs WHERE request_key=?',
                             (idempotency_key,)).fetchone()
    if row is not None and row['session_id'] != instance_id:
        raise BridgeError('idempotency_conflict', 'Request key belongs to another instance.')
    return {'instance_id': instance_id, 'found': row is not None,
            'message': bridge.message_get(row['message_id']) if row is not None else None}
