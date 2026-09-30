"""Inspect the SDK-owned Store layout without initializing or migrating any database."""

import os
from pathlib import Path
import stat

from ..errors import BridgeError


def inspect(root):
    root = Path(root).absolute()
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise BridgeError('native_identity_unknown', 'The Store location cannot be verified.')
    if not root.exists():
        return {'state': 'absent'}
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BridgeError('native_identity_unknown', 'The Store location cannot be verified.')
    database = root / 'bridge.sqlite3'
    if not database.exists():
        if any(root.iterdir()):
            raise BridgeError('native_identity_unknown', 'The Store layout is not recognized.')
        return {'state': 'absent'}
    info = database.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BridgeError('native_identity_unknown', 'The Store database cannot be verified.')
    return {'state': 'present'}
