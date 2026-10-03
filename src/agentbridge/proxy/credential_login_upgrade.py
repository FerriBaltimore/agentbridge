"""Admit state-owned login references without executing historical runtime code."""

from hashlib import sha256
import os
from pathlib import Path
import re
import stat

from ..errors import BridgeError


# Reviewed stateless source closure from the SDK bundle lock before browser runtimes.
# The adapter imported only the two modules below; neither persisted credentials.
# Keep the full source digest: a shortened directory suffix alone is not provenance.
HISTORICAL_COMMIT = 'd60873c999b7ee5215eb8cfa7bb65326175ff2f6'
HISTORICAL_DIGEST = 'e2eeeaaae72f60171e3365b3c70726adbb5e2fcddddac43ffe746fdce31365f0'
HISTORICAL_FILES = {
    'LICENSE': '909350af40c822e47840abe9f36dea176c5c8d0feec0bb82a5c6d54cb34763f6',
    'scripts/agentbridge-proxy-adapter.mjs':
        'a34c4fb5ff80558fdd6933947fea7eddd6401fdde624109af238530dcc233fc4',
    'src/agentbridge-proxy-errors.mjs':
        'dfa63fb95c8b55bab9e328cc143ae8d99e19bca3cd4c92621a37fca52238534a',
    'src/agentbridge-proxy.mjs':
        '160a3057b7e193142ce92f58ef508abd537ef864bc1c8fcdf1821496c018ca1e',
}
ADAPTER = Path('scripts/agentbridge-proxy-adapter.mjs')


def unsupported():
    raise BridgeError('credential_snapshot_unsupported',
                      'The historical login runtime has not been verified.')


def bundled_alias(value, root, allowed, read_digest):
    """Only SDK cache paths qualify; unknown references remain visible to inventory."""
    if not isinstance(value, str) or len(value) > 4096 or '\x00' in value:
        return False
    path, root = Path(value), Path(root)
    cache = root / 'bundled-runtimes'
    if (not path.is_absolute() or str(path) != value or not path.is_relative_to(cache)
            or len(path.relative_to(cache).parts) != 3 or path.parts[-2:] != ADAPTER.parts
            or not re.fullmatch(r'grantbridge-[A-Za-z0-9][A-Za-z0-9._-]{0,79}-[a-f0-9]{12}',
                                path.parent.parent.name)):
        return False
    runtime = path.parent.parent
    try:
        if path.resolve(strict=False) != path:
            unsupported()
        for parent in (path, path.parent, runtime, cache, root):
            if parent.is_symlink():
                unsupported()
            if parent.exists():
                info = parent.stat()
                if info.st_uid != os.getuid() or info.st_mode & 0o022:
                    unsupported()
                if parent != path and not stat.S_ISDIR(info.st_mode):
                    unsupported()
        historical = f'grantbridge-{HISTORICAL_COMMIT}-{HISTORICAL_DIGEST[:12]}'
        if runtime.name == historical:
            combined = b''.join(name.encode() + b'\0' + digest.encode() + b'\n'
                                for name, digest in sorted(HISTORICAL_FILES.items()))
            if sha256(combined).hexdigest() != HISTORICAL_DIGEST:
                unsupported()
            if not runtime.exists():
                return True
            # A partial or changed historical cache is not equivalent to an absent cache.
            from ..bundle.runtime import _file_hashes

            if _file_hashes(runtime) != HISTORICAL_FILES:
                unsupported()
            return True
        # No adjacent code is executed: this uses the existing explicit-alias byte rule.
        if read_digest(value) not in allowed:
            unsupported()
        return True
    except (OSError, ValueError, RuntimeError):
        unsupported()
