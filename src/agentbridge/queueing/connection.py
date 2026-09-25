"""Private local control channel for a detached conversation dispatcher."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from ..errors import BridgeError
from ..models import identifier
from ..security import base_environment


MAX_REQUEST_BYTES = 32 * 1024 * 1024
_CHILDREN = []


def directory(root, instance_id):
    identifier(instance_id)
    parent = Path(root) / 'queue-runtime'
    for path in (parent, parent / instance_id):
        if path.is_symlink():
            raise BridgeError('unsafe_store', 'Queue runtime directories cannot be symbolic links.')
        path.mkdir(mode=0o700, exist_ok=True)
        if not path.is_dir() or path.stat().st_uid != os.getuid():
            raise BridgeError('unsafe_store', 'Queue runtime must belong to this user.')
        os.chmod(path, 0o700)
    return parent / instance_id


@contextmanager
def address(folder):
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        yield f'/proc/self/fd/{descriptor}/control.sock'
    finally:
        os.close(descriptor)


def referenced_secrets(bridge):
    result = {}
    for account in bridge.accounts():
        for key in (account.key_env, account.management_key_env):
            if key and os.environ.get(key):
                result[key] = os.environ[key]
    return result


def request(root, instance_id, payload, *, start=True):
    folder = directory(root, instance_id)
    encoded = (json.dumps(payload, ensure_ascii=False, allow_nan=False) + '\n').encode()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise BridgeError('invalid_request', 'Private queue input exceeds its size limit.')
    started = False
    deadline = time.monotonic() + 10
    while True:
        try:
            with address(folder) as path, socket.socket(socket.AF_UNIX) as channel:
                channel.settimeout(10)
                channel.connect(path)
                channel.sendall(encoded)
                with channel.makefile('rb') as stream:
                    value = json.loads(stream.readline(4096))
                if value.get('ok') is not True:
                    raise BridgeError(value.get('code', 'queue_dispatcher_unavailable'),
                                      'The queue dispatcher could not accept private input.')
                return
        except (FileNotFoundError, ConnectionRefusedError):
            if not start:
                return
            if not started:
                for process in list(_CHILDREN):
                    if process.poll() is not None:
                        process.wait()
                        _CHILDREN.remove(process)
                env = base_environment()
                env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
                _CHILDREN.append(subprocess.Popen(
                    [sys.executable, '-P', '-m', 'agentbridge.queueing.worker', str(root), instance_id],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, env=env))
                started = True
            if time.monotonic() >= deadline:
                break
            time.sleep(.03)
        except (OSError, ValueError):
            break
    raise BridgeError('queue_dispatcher_unavailable',
                      'The message remains saved. Resume the queue to reconnect its dispatcher.')
