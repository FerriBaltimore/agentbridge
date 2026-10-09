"""Explicit recovery stops one recorded execution; it never starts or replays work."""

import json
import os
import signal
import time

from .checkpoint.processes import descendants
from .errors import BridgeError
from .models import TERMINAL, identifier
from .native_control import cleanup
from .process import alive
from .queueing.records import set_paused


def unverified():
    return BridgeError('native_stop_unverified',
                       'The selected execution could not be confirmed stopped.',
                       phase='execution', outcome='unknown')


def recover(bridge, turn_id):
    identifier(turn_id)
    with bridge.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM runs WHERE id=?', (turn_id,)).fetchone()
        if row is None:
            raise BridgeError('not_found', 'Turn does not exist.')
        run = dict(row)
        latest = db.execute('SELECT id FROM runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1',
                            (run['session_id'],)).fetchone()
        if latest['id'] != turn_id:
            raise BridgeError('turn_conflict', 'A newer turn belongs to this conversation.')
        set_paused(bridge.store, db, run['session_id'], True, 'explicit_recovery')
        if run['state'] not in TERMINAL:
            db.execute('UPDATE runs SET stop_requested=1,updated=? WHERE id=?',
                       (time.time(), turn_id))
    deadline = time.monotonic() + 10
    escalate = time.monotonic() + min(3, json.loads(run['options'])['stop_grace'] + 1)
    observed = {}
    while True:
        current = bridge.store.get('runs', turn_id)
        for prefix in ('worker', 'child'):
            pid, token = current[prefix + '_pid'], current[prefix + '_identity']
            original = run[prefix + '_pid']
            if original and (pid, token) != (original, run[prefix + '_identity']):
                raise unverified()
            if pid is not None and not token:
                raise unverified()
            if alive(pid, token):
                observed[pid] = token
                observed.update(descendants(pid))
        living = {pid: token for pid, token in observed.items() if alive(pid, token)}
        if not living:
            # A not-yet-claimed worker may still launch. Its absence has no
            # process identity to verify; do not turn that race into permission.
            if not observed and current['state'] not in TERMINAL:
                raise unverified()
            bridge.recover(turn_id=turn_id)
            cleanup(bridge.store, current)
            session = bridge.get_session(run['session_id'])
            return {'instance_id': run['session_id'], 'turn_id': turn_id,
                    'native_session_id': session['native_id'], 'execution_stopped': True}
        if time.monotonic() >= deadline:
            raise unverified()
        if time.monotonic() >= escalate:
            # Leave the worker alive to drain output, reap children and persist
            # its unknown outcome. Every signal is bound to an observed child,
            # never to an account namespace or another turn's owner.
            owner = current['worker_pid']
            for pid, token in living.items():
                if pid == owner or not alive(pid, token):
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    raise unverified() from None
        time.sleep(.05)
