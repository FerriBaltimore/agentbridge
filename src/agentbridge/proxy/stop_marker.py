"""Durable evidence that one managed sidecar process group was stopped."""

import json
import os
from pathlib import Path
import secrets
import stat

from ..errors import BridgeError


_NAME = 'stop-confirmed.json'


def read_stop_marker(account_dir, account_id):
    path = account_dir / _NAME
    if not path.exists():
        return False
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise BridgeError('managed_proxy_state_invalid',
                          'The managed proxy stop marker is unsafe.')
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError):
        raise BridgeError('managed_proxy_state_invalid',
                          'The managed proxy stop marker is invalid.') from None
    if (not isinstance(value, dict) or set(value) !=
            {'version', 'account_id', 'pid', 'start', 'port'} or value['version'] != 1
            or value['account_id'] != account_id or type(value['pid']) is not int
            or value['pid'] <= 1 or not isinstance(value['start'], str)
            or not value['start'] or type(value['port']) is not int
            or not 1 <= value['port'] <= 65535):
        raise BridgeError('managed_proxy_state_invalid',
                          'The managed proxy stop marker is invalid.')
    return True


def write_stop_marker(account_dir, account_id, record):
    """Publish confirmed stop before deleting the route, with directory fsync."""
    value = {'version': 1, 'account_id': account_id, 'pid': record['pid'],
             'start': record['start'], 'port': record['port']}
    temporary = account_dir / f'.stop-{secrets.token_hex(8)}'
    target = account_dir / _NAME
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        sync_directory(account_dir)
    finally:
        temporary.unlink(missing_ok=True)


def clear_stop_marker(account_dir):
    (account_dir / _NAME).unlink(missing_ok=True)
    sync_directory(account_dir)


def sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
