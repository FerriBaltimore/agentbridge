"""Legacy owner records and locks supplement the host-verified closed process domain."""

from contextlib import contextmanager
import fcntl
import os
import stat

from ..errors import BridgeError
from ..process import alive


def denied():
    raise BridgeError('store_migration_busy',
                      'Stop Store workers, dispatchers and credential owners before migration.')


def recorded_owners(connection):
    if connection.execute("SELECT 1 FROM runs WHERE state IN "
                          "('starting','running','stopping') LIMIT 1").fetchone():
        denied()
    for row in connection.execute('SELECT worker_pid,worker_identity,child_pid,child_identity '
                                  'FROM runs'):
        for pid, recorded in ((row[0], row[1]), (row[2], row[3])):
            if pid and (not recorded or alive(pid, recorded)):
                denied()
    for pid, recorded in connection.execute('SELECT dispatcher_pid,dispatcher_identity '
                                            'FROM conversation_queues'):
        if pid and (not recorded or alive(pid, recorded)):
            denied()


@contextmanager
def legacy_locks(root):
    """Hold existing supervisor/dispatcher locks throughout the migration."""
    descriptors = []
    try:
        candidates = [*root.glob('managed-proxies/supervisor.lock'),
                      *root.glob('queue-runtime/*/owner.lock')]
        for path in candidates:
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                denied()
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                denied()
        yield
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
