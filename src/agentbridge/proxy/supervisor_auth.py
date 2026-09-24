"""Ephemeral authority for the local sidecar supervisor socket.

The secret lives only in a sealed supervisor memfd. Trusted AgentBridge clients
read it through procfs; isolated native providers cannot inspect that process.
"""

import fcntl
from hmac import compare_digest
import os
from pathlib import Path
import re
import secrets
import socket
import struct

from ..errors import BridgeError


_NAME = 'agentbridge-supervisor-auth'
_TOKEN = re.compile(r'[A-Za-z0-9_-]{64}\Z')


def create_auth():
    if not hasattr(os, 'memfd_create'):
        raise BridgeError('unsupported_platform', 'The local supervisor requires Linux memfd support.')
    descriptor = os.memfd_create(_NAME, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        token = secrets.token_urlsafe(48)
        os.write(descriptor, token.encode('ascii'))
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE |
                    fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, token


def same_user_pid(connection):
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    pid, uid, _ = struct.unpack('3i', raw)
    if pid <= 1 or uid != os.getuid():
        raise BridgeError('managed_proxy_unavailable', 'The local supervisor peer is unavailable.')
    return pid


def read_auth(pid, descriptor):
    if type(descriptor) is not int or not 3 <= descriptor <= 65535:
        raise BridgeError('managed_proxy_protocol_error', 'The local supervisor auth descriptor is invalid.')
    path = Path(f'/proc/{pid}/fd/{descriptor}')
    try:
        if _NAME not in os.readlink(path):
            raise ValueError('unexpected descriptor')
        with path.open('rb') as stream:
            raw = stream.read(65)
        token = raw.decode('ascii')
        if _TOKEN.fullmatch(token) is None:
            raise ValueError('invalid token')
        return token
    except (OSError, UnicodeError, ValueError):
        raise BridgeError('managed_proxy_unavailable',
                          'The local supervisor authority is unavailable.') from None


def authorized(supplied, expected):
    return isinstance(supplied, str) and _TOKEN.fullmatch(supplied) is not None \
        and compare_digest(supplied, expected)
