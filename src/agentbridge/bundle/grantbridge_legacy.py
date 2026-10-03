"""Verified stateless proxy adapter shipped in AgentBridge 2.9.1.

Keep bound account routes admissible across the browser runtime upgrade. This exact
closure never wrote GrantBridge credentials. Unknown or modified closures remain refused.
"""

import json
from pathlib import Path

from .runtime import _file_hashes
from ..errors import BridgeError


VERSION = '1.0.0-rc.3'
DIGEST = '607a5541f8f08bf4abf021eea891c84e2f830711551ba2f6146209f211f1dfd1'
FILES = {
    'LICENSE': '909350af40c822e47840abe9f36dea176c5c8d0feec0bb82a5c6d54cb34763f6',
    'scripts/agentbridge-proxy-adapter.mjs':
        '312cd70c6a098bfb9b58e47ae2e78ad8e0367045559d56e3acd556a637b7dd61',
    'src/agentbridge-proxy-errors.mjs':
        'dfa63fb95c8b55bab9e328cc143ae8d99e19bca3cd4c92621a37fca52238534a',
    'src/agentbridge-proxy.mjs':
        'bd53b3fc98d2e991e486e2923fe741aa4424078c3a75996b17dbd2cbe7faf22f',
}


def verified_legacy_adapter(adapter, state_root):
    root = Path(state_root) / 'bundled-runtimes' / f'grantbridge-{VERSION}-{DIGEST[:12]}'
    if adapter != root / 'scripts/agentbridge-proxy-adapter.mjs':
        return False
    try:
        if root.is_symlink() or root.parent.is_symlink() or not root.is_dir():
            return False
        marker = root / '.verified.json'
        if marker.is_symlink() or marker.stat().st_size > 8192:
            return False
        return (_file_hashes(root) == FILES and json.loads(marker.read_text()) == {
            'component': 'grantbridge', 'digest': DIGEST, 'files': FILES})
    except (OSError, ValueError, BridgeError):
        return False
