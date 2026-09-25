"""One-use live inputs: durable intent, observed acknowledgement, no replay."""

import json
import time

from ..attachments import descriptors, images, text_prompt
from .records import changed, set_paused


class Steering:
    def __init__(self, store, turn_id):
        self.store, self.turn_id = store, turn_id

    def take(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute('SELECT * FROM runs WHERE id=?', (self.turn_id,)).fetchone()
            if not run or run['state'] != 'running' or run['stop_requested']:
                return None
            row = db.execute("SELECT * FROM queued_messages WHERE turn_id=? AND state='steering' "
                             "ORDER BY updated,id LIMIT 1", (self.turn_id,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE queued_messages SET state='delivering',updated=? WHERE id=?",
                       (time.time(), row['id']))
            changed(self.store, db, row['session_id'], 'delivering', row['id'], self.turn_id)
        attachments = json.loads(row['options']).get('attachments', [])
        content = [{'type': 'text', 'text': text_prompt(row['content'], attachments)}]
        content.extend({'type': 'image', 'url': 'data:' + item['media_type'] + ';base64,' + item['data']}
                       for item in images(attachments))
        return row['id'], content

    def acknowledge(self, message_id, *, accepted, code=None):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM queued_messages WHERE id=? AND turn_id=?',
                             (message_id, self.turn_id)).fetchone()
            if row is None or row['state'] != 'delivering':
                return
            state = 'unknown' if accepted is None else 'delivered' if accepted else 'rejected'
            db.execute('UPDATE queued_messages SET state=?,error=?,updated=? WHERE id=?',
                       (state, code, time.time(), message_id))
            if accepted:
                attachments = json.loads(row['options']).get('attachments', [])
                self.store._event(db, self.turn_id, row['session_id'], 'user', {
                    'message_id': message_id, 'text': row['content'],
                    'attachments': descriptors(attachments),
                    'attachment_content_omitted': bool(attachments), 'delivery': 'steer'})
            changed(self.store, db, row['session_id'], state, message_id, self.turn_id)
            if state == 'unknown':
                set_paused(self.store, db, row['session_id'], True, 'unknown_outcome')
