"""Linux worker-owned descendant cleanup, including children that call setsid()."""

import ctypes
import os
from pathlib import Path
import signal
import sys
import time

from ..errors import BridgeError
from ..process import alive, identity


def enable_subreaper():
    if not sys.platform.startswith('linux'):
        raise BridgeError('checkpoint_incompatible', 'Durable execution requires Linux supervision.')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise BridgeError('checkpoint_incompatible', 'Native descendant supervision is unavailable.')


def descendants(parent):
    children = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] != 'Z':
                children.setdefault(int(fields[1]), []).append(int(entry.name))
        except FileNotFoundError:
            continue  # A child can exit between /proc enumeration and observation.
        except (OSError, ValueError, IndexError):
            raise BridgeError('checkpoint_busy', 'Native process absence cannot be verified.')
    found, queue = {}, [parent]
    while queue:
        current = queue.pop()
        for child in children.get(current, []):
            if child not in found:
                found[child] = identity(child)
                queue.append(child)
    return found


def quiesce(timeout=3):
    """Only this worker's children are signalled, guarded against PID reuse.

    The worker becomes a subreaper before launching native code. Its orphan descendants
    stay parented here; other instances have different workers and are never traversed.
    """
    deadline = time.monotonic() + timeout
    while True:
        observed = descendants(os.getpid())
        if not observed:
            return True
        for pid, recorded in observed.items():
            if recorded and alive(pid, recorded):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(.02)
