"""Choose private default state outside the selected project workspace."""

import os
from pathlib import Path

from .errors import BridgeError


def default_root():
    """Do not silently abandon an existing project-local v1/v2 state."""
    previous = Path.cwd() / '.agentbridge'
    if (previous / 'bridge.sqlite3').exists():
        raise BridgeError('state_migration_required',
                          'Move the existing .agentbridge state outside the workspace '
                          'before using the new default, or pass an explicit --root.')
    configured = os.environ.get('XDG_STATE_HOME')
    base = Path(configured).expanduser() if configured else Path.home() / '.local/state'
    if not base.is_absolute():
        raise BridgeError('invalid_environment', 'XDG_STATE_HOME must be an absolute path.')
    return base / 'agentbridge'
