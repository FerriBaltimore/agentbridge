"""Local migration exclusion and rejection of already-open, superseded Store clients."""

from contextlib import contextmanager
import fcntl
import os
import stat
import time

from ..errors import BridgeError


@contextmanager
def guard(root, *, exclusive=False, timeout=10):
    descriptor = os.open(root / '.store-authority.lock',
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BridgeError('unsafe_store_configuration', 'Store authority lock must be private.')
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                            | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BridgeError('store_migration_busy', 'Store connections must drain first.')
                time.sleep(.01)
        yield
    finally:
        os.close(descriptor)


def require_ready(selected):
    if selected and selected.get('recovery_operation'):
        raise BridgeError('store_recovery_pending', 'Complete the host Store recovery before use.')
    if selected and selected.get('migration_operation'):
        raise BridgeError('store_migration_pending', 'Complete the host Store migration before use.')


def require_selected(root, storage):
    from .selection import load

    selected = load(root)
    require_ready(selected)
    expected = {'format_version': '1', 'backend': storage.name}
    if storage.name == 'postgresql':
        expected.update(storage.configuration.document())
    if selected != expected:
        raise BridgeError('store_authority_changed', 'Reopen this Store using its selected backend.')
