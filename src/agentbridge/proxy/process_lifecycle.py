"""Verify and stop one saved CLIProxyAPI process identity."""

import os
from pathlib import Path
import signal
import time

from ..errors import BridgeError


def _pid_start(pid):
    try:
        value = Path(f'/proc/{pid}/stat').read_text()
        return value[value.rfind(')') + 2:].split()[19]
    except (OSError, IndexError):
        return None


def _same_process(record, account_dir):
    pid = record.get('pid')
    if not isinstance(pid, int) or pid <= 1 or _pid_start(pid) != record.get('start'):
        return False
    try:
        process = Path(f'/proc/{pid}')
        if process.stat().st_uid != os.getuid() or (process / 'cwd').resolve() != account_dir:
            return False
        arguments = (process / 'cmdline').read_bytes().split(b'\0')
        binary = Path(record['binary']).resolve()
        return any(Path(os.fsdecode(arg)).resolve() == binary for arg in arguments[:2] if arg)
    except (OSError, KeyError, ValueError):
        return False


def _record_process_running(record):
    """Check the saved PID identity, treating unreadable state as unknown."""
    pid, start = record.get('pid'), record.get('start')
    if not isinstance(pid, int) or pid <= 1 or not isinstance(start, str) or not start:
        raise BridgeError('managed_proxy_stop_unverified',
                          'The managed proxy process identity is unavailable.')
    try:
        value = Path(f'/proc/{pid}/stat').read_text()
    except FileNotFoundError:
        return False
    except OSError:
        raise BridgeError('managed_proxy_stop_unverified',
                          'The managed proxy process state could not be verified.') from None
    fields = value[value.rfind(')') + 2:].split()
    if len(fields) < 20:
        raise BridgeError('managed_proxy_stop_unverified',
                          'The managed proxy process state is invalid.')
    return fields[19] == start and fields[0] not in ('Z', 'X')


def _group_running(record):
    """A sidecar stop includes descendants in its dedicated process group."""
    pid = record['pid']
    current_start = _pid_start(pid)
    if current_start is not None and current_start != record['start']:
        raise BridgeError('managed_proxy_stop_unverified',
                          'The managed proxy process identity changed.')
    try:
        processes = tuple(Path('/proc').iterdir())
        for process in processes:
            if not process.name.isdecimal():
                continue
            try:
                value = (process / 'stat').read_text()
            except FileNotFoundError:
                continue
            fields = value[value.rfind(')') + 2:].split()
            if len(fields) < 20:
                raise ValueError('invalid process state')
            if fields[2] == str(pid) and fields[0] not in ('Z', 'X'):
                return True
    except (OSError, ValueError):
        raise BridgeError('managed_proxy_stop_unverified',
                          'The managed proxy process group could not be verified.') from None
    return False


def _stop_record(record, account_dir):
    if not _record_process_running(record):
        if _group_running(record):
            raise BridgeError('managed_proxy_stop_unverified',
                              'The managed proxy leader is gone but its group remains.')
        return
    if not _same_process(record, account_dir):
        raise BridgeError('managed_proxy_stop_unverified',
                          'The managed proxy process could not be matched safely.')
    pid = record['pid']
    try:
        if os.getpgid(pid) != pid:
            raise BridgeError('managed_proxy_stop_unverified',
                              'The managed proxy process group is not isolated.')
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        if not _group_running(record):
            return
        raise BridgeError('managed_proxy_stop_unverified',
                          'The managed proxy process group could not be stopped.') from None
    except OSError:
        raise BridgeError('managed_proxy_stop_unverified',
                          'The managed proxy process group could not be stopped.') from None
    for _ in range(30):
        if not _group_running(record):
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        if not _group_running(record):
            return
        raise BridgeError('managed_proxy_stop_unverified',
                          'The managed proxy process group could not be stopped.') from None
    except OSError:
        raise BridgeError('managed_proxy_stop_unverified',
                          'The managed proxy process group could not be stopped.') from None
    for _ in range(20):
        if not _group_running(record):
            return
        time.sleep(0.05)
    raise BridgeError('managed_proxy_stop_unverified',
                      'The managed proxy process is still running after stop.')
