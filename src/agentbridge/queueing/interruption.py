"""The existing native owner consumes one durable targeted queue interruption."""

import json
import os
import time

from ..errors import BridgeError
from .records import active, changed, set_paused


class QueuedInterruption:
    def __init__(self, store, run_id, control):
        self.store, self.run_id, self.control = store, run_id, control
        self.attempted = set()

    def poll(self):
        control = self.control
        if control.done or control.thread_id is None or control.turn_id is None:
            return
        target = self._take()
        if target is None:
            return
        self.attempted.add(target['id'])
        error = None
        try:
            control.rpc('turn/interrupt', {'threadId': control.thread_id,
                                           'turnId': control.turn_id}, timeout=2)
        except BridgeError as failure:
            error = failure.code
        self._record(target, error)

    def _take(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute('SELECT * FROM runs WHERE id=?', (self.run_id,)).fetchone()
            if run is None or run['child_pid'] != os.getpid():
                return None
            current = active(db, run['session_id'])
            session = db.execute('SELECT account_id FROM sessions WHERE id=?',
                                 (run['session_id'],)).fetchone()
            if (current is None or current['id'] != self.run_id or run['stop_requested']
                    or session is None or session['account_id'] != run['account_id']):
                return None
            row = db.execute("SELECT * FROM queued_messages WHERE session_id=? "
                             "AND target_turn_id=? AND delivery='interrupt' AND state='queued'",
                             (run['session_id'], self.run_id)).fetchone()
            if row is None or row['id'] in self.attempted:
                return None
            # Events cannot be pruned while an instance owns an active native process.
            events = db.execute("SELECT data FROM events WHERE run_id=? AND kind='queue_changed'",
                                (self.run_id,)).fetchall()
            receipts = [json.loads(event['data']) for event in events]
            if any(event.get('action') == 'native_interrupt_sent'
                   and event.get('message_id') == row['id'] for event in receipts):
                self.attempted.add(row['id'])
                return None
            changed(self.store, db, run['session_id'], 'native_interrupt_sent', row['id'],
                    self.run_id)
            return dict(row)

    def _record(self, target, error):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute('SELECT * FROM runs WHERE id=?', (self.run_id,)).fetchone()
            if run is None or run['child_pid'] != os.getpid():
                return
            current = db.execute('SELECT * FROM queued_messages WHERE id=?',
                                 (target['id'],)).fetchone()
            if (current is None or current['session_id'] != target['session_id']
                    or current['target_turn_id'] != self.run_id
                    or current['delivery'] != 'interrupt' or current['state'] != 'queued'):
                return
            action = 'native_interrupt_unknown' if error else 'native_interrupt_acknowledged'
            changed(self.store, db, target['session_id'], action, target['id'], self.run_id)
            if error:
                db.execute('UPDATE queued_messages SET error=?,updated=? WHERE id=?',
                           (error, time.time(), target['id']))
                set_paused(self.store, db, target['session_id'], True, action)


def observed_interruption(db, run_id):
    """Process-loss interruption is not a native acknowledgement of termination."""
    row = db.execute("SELECT data FROM events WHERE run_id=? AND kind='native_turn_completed' "
                     'ORDER BY seq DESC LIMIT 1', (run_id,)).fetchone()
    return bool(row and json.loads(row['data']).get('turn', {}).get('status') == 'interrupted')
