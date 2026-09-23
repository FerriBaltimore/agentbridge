"""Resolve only the local proxy client key for a Codex execution."""

import os

from .errors import BridgeError
from .transports import require_proxy_account


def environment(account):
    require_proxy_account(account)
    if not account.key_env:
        raise BridgeError('credential_unavailable', 'The local proxy client key is unavailable.')
    value = os.environ.get(account.key_env)
    if not value or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise BridgeError('credential_unavailable', 'The local proxy client key is unavailable.')
    return {account.key_env: value}
