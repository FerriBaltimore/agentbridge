"""Launch a detached run owner, with private input and conservative failures."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .errors import BridgeError
from .process import alive, identity
from .security import base_environment
from .store import dumps
from .execution_context import PRIVATE_EXECUTION_KEY


CLEANUP_GRACE_SECONDS = 5


def _descendants(pid):
    """Linux child identities cover native spawn before its store update."""
    pending, observed = [pid], {}
    while pending and len(observed) < 4096:
        parent = pending.pop()
        try:
            children = Path(f'/proc/{parent}/task/{parent}/children').read_text().split()
        except OSError:
            continue
        for value in children:
            child = int(value)
            token = identity(child)
            if child not in observed and token:
                observed[child] = token
                pending.append(child)
    return observed


def _remember(store, run_id, process, observed):
    observed.update(_descendants(process.pid))
    try:
        row = store.get('runs', run_id)
        if alive(row['child_pid'], row['child_identity']):
            observed.setdefault(row['child_pid'], row['child_identity'])
    except Exception:
        # Cleanup must still work if the store failed during native launch.
        pass


def _cleanup(store, run_id, process):
    observed = {}
    _remember(store, run_id, process, observed)
    _close_input(process.stdin)
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + CLEANUP_GRACE_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        _remember(store, run_id, process, observed)
        try:
            process.wait(timeout=min(.05, max(0, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is None:
        # Freeze the owner before the final descendant snapshot and escalation.
        try:
            process.send_signal(signal.SIGSTOP)
        except ProcessLookupError:
            pass
    _remember(store, run_id, process, observed)
    for pid, token in observed.items():
        if not alive(pid, token):
            continue
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)
    deadline = time.monotonic() + 1
    while any(alive(pid, token) for pid, token in observed.items()):
        if time.monotonic() >= deadline:
            return False
        time.sleep(.01)
    return True


def _close_input(stream):
    # Close the raw pipe first, without flushing incomplete input again.
    try:
        getattr(stream, 'raw', stream).close()
    except (OSError, ValueError):
        pass
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _finish(store, run_id, state, code):
    try:
        # Store.finish atomically preserves any terminal result already saved.
        store.finish(run_id, state, code)
        return True
    except Exception:
        return False


def launch(store, run_id, secrets, *, execution=None):
    """Return the run owner only after delivering its private input.

    A failure before Popen is known not to have started the worker. Once a
    process exists, delivery failure cannot establish a native operation's
    outcome, even if the worker exits while this method is writing to it.
    """
    payload = dumps({PRIVATE_EXECUTION_KEY: True, 'secrets': secrets,
                     'execution': execution}).encode() if execution else dumps(secrets).encode()
    env = base_environment()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)
    try:
        process = subprocess.Popen([sys.executable, '-P', '-m', 'agentbridge.worker', str(store.root), run_id],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError:
        persisted = _finish(store, run_id, 'failed', 'launch_failed')
        raise BridgeError('launch_failed', 'Could not start the AgentBridge worker.', phase='launch',
                          outcome='not_started', details={'turn_id': run_id, 'state_persisted': persisted}) from None
    try:
        process.stdin.write(payload)
        process.stdin.close()
        return process
    except (OSError, ValueError):
        try:
            cleaned = _cleanup(store, run_id, process)
        except (OSError, subprocess.TimeoutExpired):
            cleaned = False
        persisted = _finish(store, run_id, 'interrupted', 'unknown_outcome') if cleaned else False
        raise BridgeError('unknown_outcome', 'Worker input delivery failed. Inspect the saved turn before retrying.',
                          phase='launch', outcome='unknown', details={
                              'turn_id': run_id, 'state_persisted': persisted, 'cleanup_complete': cleaned}) from None
