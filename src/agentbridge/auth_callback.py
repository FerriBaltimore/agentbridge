"""Validate an OAuth redirect before transient GrantBridge delivery."""

from hmac import compare_digest
from urllib.parse import parse_qs, urlsplit

from .errors import BridgeError


_DESTINATIONS = {'codex': (1455, '/auth/callback'),
                 'claude': (54545, '/callback')}


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
