"""One detached dispatcher per conversation; provider turns keep their own owners."""

import fcntl
import json
import os
import selectors
import signal
import socket
import sys
import time

from ..client import Bridge
from ..errors import BridgeError
from ..execution_context import verify
from ..models import RunOptions
from ..process import alive, identity
from .connection import MAX_REQUEST_BYTES, address, directory
from .persistence import QueueStore
from .records import active, pending, queue_record


class Dispatcher:
    def __init__(self, root, instance_id):
        self.bridge = Bridge(root)
        self.queue = QueueStore(self.bridge.store)
        self.instance_id = instance_id
        self.execution = {}
        self.stopped = False
        self.next_recovery = 0
        with self.bridge.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO conversation_queues(session_id) VALUES (?)', (instance_id,))
            db.execute('UPDATE conversation_queues SET dispatcher_pid=?,dispatcher_identity=? WHERE session_id=?',
                       (os.getpid(), identity(os.getpid()), instance_id))

    def receive(self, server):
        channel, _ = server.accept()
        with channel:
            channel.settimeout(5)
            try:
                with channel.makefile('rb') as stream:
                    line = stream.readline(MAX_REQUEST_BYTES + 1)
                    if len(line) > MAX_REQUEST_BYTES or not line.endswith(b'\n'):
                        raise ValueError('invalid packet')
                    payload = json.loads(line)
                if payload.get('action') == 'shutdown':
                    self.stopped = True
                else:
                    self.bridge.get_session(self.instance_id)
                    allowed = {name for account in self.bridge.accounts()
                               for name in (account.key_env, account.management_key_env) if name}
                    secrets = payload.get('secrets', {})
                    if (not isinstance(secrets, dict) or any(key not in allowed or
                            not isinstance(value, str) or len(value) > 4096
                            for key, value in secrets.items())):
                        raise ValueError('invalid references')
                    os.environ.update(secrets)
                    message_id = payload.get('message_id')
                    if message_id:
                        row = self.queue.get(message_id)
                        if row['session_id'] != self.instance_id:
                            raise ValueError('wrong instance')
                        execution = payload.get('execution')
                        options = RunOptions(**json.loads(row['options']))
                        if options.context_package_digest or options.mcp_binding_digest:
                            if payload.get('action') == 'check':
                                if message_id not in self.execution:
                                    raise BridgeError('context_required', 'Fresh private context is required.')
                            else:
                                self.execution[message_id] = verify(options, execution)
                        if payload.get('activate', False):
                            self.queue.activate(self.instance_id, message_id)
                channel.sendall(b'{"ok":true}\n')
            except Exception as error:
                code = error.code if isinstance(error, BridgeError) else 'invalid_request'
                try:
                    channel.sendall((json.dumps({'ok': False, 'code': code}) + '\n').encode())
                except OSError:
                    pass

    def advance(self):
        store = self.bridge.store
        if time.monotonic() >= self.next_recovery:
            self.bridge.recover(instance_id=self.instance_id)
            self.next_recovery = time.monotonic() + .5
        self.bridge.close()
        with store.connect() as db:
            db.execute('BEGIN')
            queue = queue_record(db, self.instance_id)
            rows = pending(db, self.instance_id)
            running = active(db, self.instance_id)
            previous = db.execute('SELECT * FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1',
                                  (self.instance_id,)).fetchone()
        pending_ids = {row['id'] for row in rows}
        for message_id in list(self.execution):
            if message_id not in pending_ids:
                self.execution.pop(message_id, None)
        if not rows:
            return False
        if queue['paused'] or running or rows[0]['state'] != 'queued':
            return True
        # A terminal commit can precede the owner's final cleanup. Never overlap
        # native processes or change accounts while that owner is still alive.
        if previous and (alive(previous['worker_pid'], previous['worker_identity'])
                         or alive(previous['child_pid'], previous['child_identity'])):
            return True
        row = rows[0]
        options = RunOptions(**json.loads(row['options']))
        execution = self.execution.get(row['id'])
        if (options.context_package_digest or options.mcp_binding_digest) and execution is None:
            self.queue.block(self.instance_id, row['id'], 'context_required')
            return True
        try:
            self.bridge.submit(self.instance_id, row['content'], options=options,
                               message_id=row['id'], execution=execution,
                               excluded_account_refs=json.loads(row['exclusions']))
        except BridgeError as error:
            if error.code not in {'busy', 'account_busy', 'context_stale'}:
                self.queue.block(self.instance_id, row['id'], error.code)
        except Exception:
            self.queue.block(self.instance_id, row['id'], 'queue_dispatch_failed')
        return True


def main():
    os.umask(0o077)
    root, instance_id = sys.argv[1:3]
    folder = directory(root, instance_id)
    descriptor = os.open(folder / 'owner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        dispatcher = Dispatcher(root, instance_id)
        signal.signal(signal.SIGTERM, lambda *_: setattr(dispatcher, 'stopped', True))
        signal.signal(signal.SIGINT, lambda *_: setattr(dispatcher, 'stopped', True))
        with socket.socket(socket.AF_UNIX) as server, selectors.DefaultSelector() as select:
            (folder / 'control.sock').unlink(missing_ok=True)
            with address(folder) as path:
                server.bind(path)
            server.listen(16)
            select.register(server, selectors.EVENT_READ)
            idle_since = time.monotonic()
            while not dispatcher.stopped:
                for _, _ in select.select(.05):
                    dispatcher.receive(server)
                    idle_since = time.monotonic()
                try:
                    if dispatcher.advance():
                        idle_since = time.monotonic()
                except BridgeError as error:
                    if error.code in {'instance_not_found', 'not_found'}:
                        break
                    raise
                if time.monotonic() - idle_since > 2:
                    break
            (folder / 'control.sock').unlink(missing_ok=True)
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    main()
