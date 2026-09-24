"""Validate an OAuth redirect before transient GrantBridge delivery."""

import errno
from hmac import compare_digest
import socket
from urllib.parse import parse_qs, urlsplit

from .errors import BridgeError


_DESTINATIONS = {'codex': (1455, '/auth/callback'),
                 'claude': (54545, '/callback')}


def ensure_callback_port_available(provider):
    """Catch a known local bind conflict before dispatching proxy OAuth."""
    destination = _DESTINATIONS.get(provider)
    if destination is None:
        return
    port = destination[0]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            # CLIProxyAPI's browser forwarder binds 0.0.0.0 on this port.
            listener.bind(('0.0.0.0', port))
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            raise BridgeError(
                'oauth_callback_port_busy',
                f'Local OAuth callback port {port} is in use. Close the application using it and start a new login.',
            ) from None
        raise BridgeError(
            'oauth_callback_unavailable',
            f'Local OAuth callback port {port} is unavailable. Check local networking before starting a new login.',
        ) from None


def validate_callback(provider, state, redirect_url):
    target = _DESTINATIONS.get(provider)
    if (target is None or not isinstance(state, str) or not 1 <= len(state) <= 128
            or not isinstance(redirect_url, str) or not 1 <= len(redirect_url) <= 8192
            or any(ord(character) < 32 or ord(character) == 127
                   for character in redirect_url)):
        raise BridgeError('invalid_request', 'Invalid OAuth callback URL.')
    try:
        parsed = urlsplit(redirect_url)
        query = parse_qs(parsed.query, strict_parsing=True, max_num_fields=8)
        codes, states = query.get('code', []), query.get('state', [])
        valid = (parsed.scheme == 'http' and parsed.hostname in {'localhost', '127.0.0.1'}
                 and parsed.port == target[0] and parsed.path == target[1]
                 and not parsed.username and not parsed.password and not parsed.fragment
                 and len(codes) == len(states) == 1 and 1 <= len(codes[0]) <= 4096
                 and compare_digest(states[0], state))
    except (UnicodeError, ValueError):
        valid = False
    if not valid:
        raise BridgeError('invalid_request', 'Invalid OAuth callback URL.')
