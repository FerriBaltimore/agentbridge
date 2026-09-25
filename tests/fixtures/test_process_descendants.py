"""Resolve fixture namespace PIDs only within a recorded execution's descendants."""

import os
from pathlib import Path
import signal
import time

from agentbridge.process import alive, identity


def _status(pid):
    try:
        fields = {}
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            name, _, value = line.partition(':')
            if name in ('PPid', 'NSpid'):
                fields[name] = [int(item) for item in value.split()]
        return fields
    except (OSError, ValueError):
        return {}


def _children(pid, started):
    if not alive(pid, started):
        return []
    children = set()
    try:
        tasks = list(Path(f'/proc/{pid}/task').iterdir())
        for task in tasks:
            try:
                children.update(int(value) for value in (task / 'children').read_text().split())
            except (OSError, ValueError):
                continue
    except OSError:
        return []
    return sorted(children) if alive(pid, started) else []


def owned_descendant(run, namespace_pid, *, timeout=5):
    """Return a host PID and start identity, without inspecting unrelated tasks."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = run.snapshot
        root = row.get('child_pid'), row.get('child_identity')
        pending = [root] if alive(*root) else []
        visited = set()
        matches = []
        while pending and len(visited) < 100:
            parent, parent_started = pending.pop()
            if parent in visited:
                continue
            visited.add(parent)
            for child in _children(parent, parent_started):
                started = identity(child)
                status = _status(child)
                if (not started or status.get('PPid') != [parent]
                        or not alive(parent, parent_started) or not alive(child, started)):
                    continue
                pending.append((child, started))
                if status.get('NSpid', [])[-1:] == [namespace_pid]:
                    matches.append((child, started))
        if len(matches) == 1 and alive(*root) and alive(*matches[0]):
            return matches[0]
        time.sleep(.02)
    raise AssertionError('The fixture PID did not resolve to one verified execution descendant.')


def kill_owned(pid, started):
    """Use a PID descriptor so a reused host PID can never receive cleanup."""
    if not alive(pid, started):
        return
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return
    try:
        if alive(pid, started):
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
    finally:
        os.close(descriptor)
