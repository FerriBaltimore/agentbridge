"""Trusted Python children retain verified sibling dependencies from a host bundle."""

from hashlib import sha256
import json
import os
from pathlib import Path
import stat

from .errors import BridgeError


def python_path():
    """Derive paths from this package, never from the caller's PYTHONPATH.

    The host authenticates the immutable bundle before executing this package.
    Its manifest binds the optional dependency directory; ordinary installed
    wheels continue to use their interpreter's own site-packages.
    """
    source = Path(__file__).resolve().parent.parent
    root = source.parent
    manifest = root / 'SNAPSHOT-MANIFEST.json'
    dependencies = root / 'python'
    if not manifest.exists():
        return str(source)
    try:
        if manifest.is_symlink() or dependencies.is_symlink():
            raise ValueError('symbolic bundle path')
        inventory = json.loads(manifest.read_bytes())
        expected = {name: digest for name, digest in inventory.items()
                    if name.startswith('python/')}
        if not expected:
            return str(source)
        if 'python/RUNTIME.json' not in expected or not dependencies.is_dir():
            raise ValueError('incomplete dependency runtime')
        observed = set()
        for path in dependencies.rglob('*'):
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ValueError('unsafe dependency entry')
            name = path.relative_to(root).as_posix()
            if name not in expected or sha256(path.read_bytes()).hexdigest() != expected[name]:
                raise ValueError('dependency integrity mismatch')
            observed.add(name)
        if observed != expected.keys():
            raise ValueError('missing dependency entry')
        return os.pathsep.join((str(source), str(dependencies)))
    except (OSError, ValueError, TypeError, AttributeError):
        raise BridgeError('bundle_integrity_failed',
                          'The host Python dependency bundle could not be verified.') from None
