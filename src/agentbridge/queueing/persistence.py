"""Persistent queue editing with atomic order and optimistic concurrency."""

from dataclasses import asdict
import json
import time
from uuid import uuid4

from ..attachments import descriptors
from ..errors import BridgeError
from ..models import identifier, page_values
from ..process import alive
from ..store import dumps
from .records import (PENDING, changed, insert_position, item_record, pending,
                      queue_record, reorder, require_pending, set_paused)


def public_item(row):
    options = json.loads(row['options'])
    return {'message_id': row['id'], 'instance_id': row['session_id'],
            'content': row['content'], 'state': row['state'], 'position': row['position'],
            'delivery': row['delivery'], 'turn_id': row['turn_id'],
            'target_turn_id': row['target_turn_id'], 'error': row['error'],
            'created_at': row['created'], 'updated_at': row['updated'],
            'attachments': descriptors(options.get('attachments', [])),
            'context_required': bool(options.get('context_package_digest')
                                     or options.get('mcp_binding_digest'))}


class QueueStore:
    def __init__(self, store):
        self.store = store

    def snapshot(self, instance_id, *, limit=100, cursor=0):
        identifier(instance_id)
        limit, cursor = page_values(limit, cursor)
        with self.store.connect() as db:
            db.execute('BEGIN')
            queue = queue_record(db, instance_id)
            rows = pending(db, instance_id)
            inflight = db.execute("SELECT * FROM queued_messages WHERE session_id=? "
                                  "AND state IN ('steering','delivering') ORDER BY created,id",
                                  (instance_id,)).fetchall()
        selected = rows[cursor:cursor + limit]
        return {'instance_id': instance_id, 'version': queue['version'],
                'dispatcher_running': alive(queue.get('dispatcher_pid'), queue.get('dispatcher_identity')),
                'paused': bool(queue['paused']), 'reason': queue['reason'],
                'items': [dict(public_item(row), position=cursor + index)
                          for index, row in enumerate(selected)],
                'in_flight': [public_item(row) for row in inflight],
                'total': len(rows), 'has_more': cursor + limit < len(rows),
                'next_cursor': cursor + limit if cursor + limit < len(rows) else None}

    def get(self, message_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM queued_messages WHERE id=?',
                             (identifier(message_id),)).fetchone()
        if row is None:
            raise BridgeError('message_not_found', 'Queued message does not exist.')
        return dict(row)

    def replay(self, key, digest, *, legacy_digest=None):
        if key is None:
            return None
        if not isinstance(key, str) or not 1 <= len(key) <= 256:
            raise BridgeError('invalid_request', 'idempotency_key must contain 1-256 characters.')
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM queued_messages WHERE request_key=?', (key,)).fetchone()
            if db.execute('SELECT 1 FROM runs WHERE request_key=?', (key,)).fetchone():
                raise BridgeError('idempotency_conflict', 'Request key belongs to another message.')
        if row and row['request_digest'] not in (digest, legacy_digest):
            raise BridgeError('idempotency_conflict', 'Request key belongs to different input.')
        return dict(row) if row else None

    def add(self, instance_id, content, options, exclusions, key, digest, *,
            position=None, expected_version=None, expected_instance_version=None, legacy_digest=None):
        message_id = uuid4().hex
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if key:
                old = db.execute('SELECT * FROM queued_messages WHERE request_key=?', (key,)).fetchone()
                if old:
                    if old['request_digest'] not in (digest, legacy_digest):
                        raise BridgeError('idempotency_conflict', 'Request key belongs to different input.')
                    return old['id'], False
                if db.execute('SELECT 1 FROM runs WHERE request_key=?', (key,)).fetchone():
                    raise BridgeError('idempotency_conflict', 'Request key belongs to another message.')
            queue_record(db, instance_id, expected_version)
            metadata = db.execute('SELECT state,version FROM instance_metadata WHERE session_id=?',
                                  (instance_id,)).fetchone()
            if (expected_instance_version is not None
                    and expected_instance_version != (metadata['version'] if metadata else 1)):
                raise BridgeError('version_conflict', 'Instance execution policy changed before queue admission.')
            if metadata and metadata['state'] != 'active':
                raise BridgeError('instance_archived', 'Archived instances cannot accept queued messages.')
            if db.execute('SELECT 1 FROM evaluation_instances WHERE session_id=?', (instance_id,)).fetchone():
                raise BridgeError('evaluation_queue_unsupported', 'Evaluation instances accept one immediate turn.')
            ids = [row['id'] for row in pending(db, instance_id)]
            if len(ids) >= 1000:
                raise BridgeError('queue_full', 'A conversation queue accepts at most 1000 pending messages.')
            insert_position(ids, message_id, position)
            replacement = db.execute("SELECT id FROM queued_messages WHERE session_id=? "
                                     "AND delivery='interrupt' AND state IN ('staged','queued','blocked')",
                                     (instance_id,)).fetchone()
            if replacement and ids[0] != replacement['id']:
                raise BridgeError('interrupt_pending', 'An immediate replacement must remain first.')
            now = time.time()
            db.execute('''INSERT INTO queued_messages
                (id,session_id,content,options,exclusions,request_key,request_digest,
                 position,state,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                       (message_id, instance_id, content, dumps(asdict(options)), dumps(list(exclusions)),
                        key, digest, ids.index(message_id), 'staged', now, now))
            reorder(db, ids)
            changed(self.store, db, instance_id, 'added', message_id)
        return message_id, True

    def move(self, instance_id, message_id, position, *, expected_version=None):
        if type(position) is not int:
            raise BridgeError('invalid_position', 'position must be a zero-based integer.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            queue_record(db, instance_id, expected_version)
            row = item_record(db, instance_id, identifier(message_id))
            require_pending(row)
            ids = [item['id'] for item in pending(db, instance_id) if item['id'] != message_id]
            insert_position(ids, message_id, position)
            replacement = db.execute("SELECT id FROM queued_messages WHERE session_id=? "
                                     "AND delivery='interrupt' AND state IN ('staged','queued','blocked')",
                                     (instance_id,)).fetchone()
            if replacement and ids[0] != replacement['id']:
                raise BridgeError('interrupt_pending', 'An immediate replacement must remain first.')
            reorder(db, ids)
            db.execute('UPDATE queued_messages SET updated=? WHERE id=?', (time.time(), message_id))
            changed(self.store, db, instance_id, 'moved', message_id)
        return self.snapshot(instance_id)

    def delete(self, instance_id, message_id, *, expected_version=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = item_record(db, instance_id, identifier(message_id))
            if row['state'] == 'cancelled':
                return {'message_id': message_id, 'instance_id': instance_id, 'state': 'cancelled',
                        'replayed': True}
            queue_record(db, instance_id, expected_version)
            require_pending(row, allow_replacement=True)
            db.execute("UPDATE queued_messages SET state='cancelled',updated=? WHERE id=?",
                       (time.time(), message_id))
            reorder(db, [item['id'] for item in pending(db, instance_id)])
            changed(self.store, db, instance_id, 'removed', message_id)
            if row['delivery'] == 'interrupt':
                set_paused(self.store, db, instance_id, True, 'replacement_cancelled')
        return {'message_id': message_id, 'instance_id': instance_id, 'state': 'cancelled',
                'replayed': False}

    def pause(self, instance_id, *, expected_version=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            queue_record(db, instance_id, expected_version)
            set_paused(self.store, db, instance_id, True, 'user_pause')
        return self.snapshot(instance_id)

    def resume(self, instance_id, *, expected_version=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            queue_record(db, instance_id, expected_version)
            db.execute("UPDATE queued_messages SET state='queued',error=NULL,updated=? "
                       "WHERE session_id=? AND state IN ('blocked','staged')",
                       (time.time(), instance_id))
            set_paused(self.store, db, instance_id, False)
            changed(self.store, db, instance_id, 'ready')
        return self.snapshot(instance_id)

    def activate(self, instance_id, message_id):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = item_record(db, instance_id, message_id)
            if row['state'] == 'staged':
                db.execute("UPDATE queued_messages SET state='queued',updated=? WHERE id=?",
                           (time.time(), message_id))
                changed(self.store, db, instance_id, 'ready', message_id)

    def block(self, instance_id, message_id, code):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = item_record(db, instance_id, message_id)
            if row['state'] not in PENDING:
                return
            db.execute("UPDATE queued_messages SET state='blocked',error=?,updated=? WHERE id=?",
                       (code, time.time(), message_id))
            changed(self.store, db, instance_id, 'blocked', message_id)
            set_paused(self.store, db, instance_id, True, code)
