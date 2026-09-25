"""Transactional queue invariants shared by admission and queue mutations."""

import json
import time

from ..errors import BridgeError, BusyError


PENDING = ('staged', 'queued', 'blocked')
IN_FLIGHT = (*PENDING, 'steering', 'delivering')


def queue_record(db, instance_id, expected_version=None):
    instance = db.execute('SELECT 1 FROM sessions WHERE id=?', (instance_id,)).fetchone()
    if not instance:
        raise BridgeError('instance_not_found', 'Instance does not exist.')
    row = db.execute('SELECT * FROM conversation_queues WHERE session_id=?',
                     (instance_id,)).fetchone()
    value = dict(row) if row else {'session_id': instance_id, 'version': 1,
                                  'paused': 0, 'reason': None}
    if expected_version is not None:
        if type(expected_version) is not int or expected_version < 1:
            raise BridgeError('invalid_request', 'expected_version must be a positive integer.')
        if expected_version != value['version']:
            raise BridgeError('version_conflict', 'Queue changed since it was read.')
    return value


def pending(db, instance_id):
    return db.execute("SELECT * FROM queued_messages WHERE session_id=? "
                      "AND state IN ('staged','queued','blocked') ORDER BY position,id",
                      (instance_id,)).fetchall()


def occupied(db, instance_id):
    return db.execute("SELECT 1 FROM queued_messages WHERE session_id=? "
                      "AND state IN ('staged','queued','blocked','steering','delivering')",
                      (instance_id,)).fetchone() is not None


def active(db, instance_id):
    return db.execute("SELECT * FROM runs WHERE session_id=? "
                      "AND state IN ('starting','running','stopping')", (instance_id,)).fetchone()


def item_record(db, instance_id, message_id):
    row = db.execute('SELECT * FROM queued_messages WHERE id=? AND session_id=?',
                     (message_id, instance_id)).fetchone()
    if row is None:
        raise BridgeError('message_not_found', 'No matching queued message exists.')
    return row


def require_pending(row):
    if row['state'] not in PENDING:
        raise BridgeError('message_not_pending', 'This message is no longer pending in the queue.')
    if row['delivery'] != 'queue':
        raise BridgeError('message_dispatching', 'Immediate delivery has already been requested.')


def reorder(db, ids):
    db.executemany('UPDATE queued_messages SET position=? WHERE id=?', enumerate(ids))


def insert_position(ids, message_id, position):
    if position is None:
        position = len(ids)
    if type(position) is not int or not 0 <= position <= len(ids):
        raise BridgeError('invalid_position', 'position must be a valid zero-based queue position.')
    ids.insert(position, message_id)
    return ids


def changed(store, db, instance_id, action, message_id=None, turn_id=None):
    db.execute('INSERT OR IGNORE INTO conversation_queues(session_id) VALUES (?)', (instance_id,))
    db.execute('UPDATE conversation_queues SET version=version+1 WHERE session_id=?', (instance_id,))
    version = db.execute('SELECT version FROM conversation_queues WHERE session_id=?',
                         (instance_id,)).fetchone()[0]
    store._event(db, turn_id or '', instance_id, 'queue_changed',
                 {'action': action, 'message_id': message_id, 'version': version})
    return version


def set_paused(store, db, instance_id, paused, reason=None):
    current = queue_record(db, instance_id)
    if bool(current['paused']) == paused and current['reason'] == reason:
        return
    db.execute('INSERT OR IGNORE INTO conversation_queues(session_id) VALUES (?)', (instance_id,))
    db.execute('UPDATE conversation_queues SET paused=?,reason=? WHERE session_id=?',
               (int(paused), reason, instance_id))
    changed(store, db, instance_id, 'paused' if paused else 'resumed')


def admit(store, db, instance_id, message_id, turn_id, prompt, options, exclusions, key):
    """Link queue input and execution in the same transaction as run admission."""
    row = db.execute('SELECT * FROM queued_messages WHERE id=?', (message_id,)).fetchone()
    if row is None:
        if occupied(db, instance_id):
            raise BusyError()
        if key and db.execute('SELECT 1 FROM queued_messages WHERE request_key=?', (key,)).fetchone():
            raise BridgeError('idempotency_conflict', 'Request key belongs to a queued message.')
        return
    from dataclasses import asdict
    if (row['session_id'] != instance_id or row['content'] != prompt
            or json.loads(row['options']) != asdict(options)
            or json.loads(row['exclusions']) != list(exclusions)):
        # JSON arrays and dataclass tuples need the same canonical representation.
        expected = json.loads(json.dumps(asdict(options)))
        if (row['session_id'] != instance_id or row['content'] != prompt
                or json.loads(row['options']) != expected
                or json.loads(row['exclusions']) != list(exclusions)):
            raise BridgeError('idempotency_conflict', 'Queued input cannot change during admission.')
    queue = queue_record(db, instance_id)
    head = pending(db, instance_id)
    if queue['paused'] or row['state'] != 'queued' or not head or head[0]['id'] != message_id:
        raise BusyError()
    if active(db, instance_id):
        raise BusyError()
    db.execute("UPDATE queued_messages SET state='dispatched',turn_id=?,updated=? WHERE id=?",
               (turn_id, time.time(), message_id))
    changed(store, db, instance_id, 'dispatched', message_id, turn_id)


def finished(store, db, run, state):
    """Unknown delivery is never replayed. Failed work pauses subsequent input."""
    rows = db.execute("SELECT * FROM queued_messages WHERE turn_id=? "
                      "AND state IN ('steering','delivering')", (run['id'],)).fetchall()
    for row in rows:
        outcome = 'unknown' if row['state'] == 'delivering' else 'rejected'
        db.execute('UPDATE queued_messages SET state=?,error=?,updated=? WHERE id=?',
                   (outcome, 'unknown_outcome' if outcome == 'unknown' else 'turn_finished',
                    time.time(), row['id']))
        changed(store, db, run['session_id'], outcome, row['id'], run['id'])
    replacement = db.execute("SELECT 1 FROM queued_messages WHERE session_id=? "
                             "AND state='queued' AND delivery='interrupt' AND target_turn_id=?",
                             (run['session_id'], run['id'])).fetchone()
    if state != 'completed' and not (state == 'cancelled' and replacement):
        if occupied(db, run['session_id']):
            set_paused(store, db, run['session_id'], True, 'previous_turn_' + state)
