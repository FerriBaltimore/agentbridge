"""Where the user completes a GrantBridge proxy login: this host or a phone.

`same_host` keeps the desktop path unchanged: the caller opens `authorization_url` in
whatever browser it has, which may reuse a provider session already signed in.
`isolated` is the clean same-host entry: the host process opens the URL in a fresh,
disposable browser profile (`agentbridge.auth_browser.IsolatedAuthBrowser`) so the person
types the account they mean; the wire request to GrantBridge is the same-host one.
A phone (`mobile`) opens the https authorization URL itself (`mode='browser'`) or drives
GrantBridge's hosted browser (`mode='hosted'`). A hosted login may add the client's
`viewport` so the hosted page renders at the person's real size and pixel density.
Contract: docs/interface/mobile-login.md.
"""

import math

from . import auth_contract
from .errors import BridgeError

BROWSERS = ('same_host', 'isolated', 'mobile')
MODES = ('browser', 'hosted')
# The same-host wire request also serves the isolated profile; GrantBridge never learns it.
LOCAL_BROWSERS = frozenset({'same_host', 'isolated'})
# Client viewport for a hosted browser: CSS pixels and device pixel ratio. GrantBridge applies
# the same bounds; without a viewport it keeps its own default (390×760 at scale 1).
VIEWPORT_BOUNDS = {'width': (320, 1280), 'height': (480, 1280), 'scale': (1, 3)}


def validate_entry(mode, browser):
    if browser not in BROWSERS or mode not in MODES:
        raise BridgeError('invalid_request',
                          'browser must be same_host, isolated or mobile; mode must be browser or hosted.')
    if mode == 'hosted' and browser != 'mobile':
        raise BridgeError('unsupported_operation', 'A hosted browser is only offered to a mobile login.')


def validate_viewport(viewport, mode):
    """The client's screen for a hosted login, or None when GrantBridge keeps its default."""
    if viewport is None:
        return None
    if mode != 'hosted':
        raise BridgeError('invalid_params', 'viewport is only accepted for a hosted browser login.')
    if not isinstance(viewport, dict) or set(viewport) != set(VIEWPORT_BOUNDS):
        raise BridgeError('invalid_params',
                          'viewport must be an object with width, height and scale.')
    for field, (low, high) in VIEWPORT_BOUNDS.items():
        value = viewport[field]
        whole = type(value) is int
        number = whole or (type(value) is float and math.isfinite(value))
        if not (number if field == 'scale' else whole) or not low <= value <= high:
            kind = 'a number' if field == 'scale' else 'a whole number'
            raise BridgeError('invalid_params',
                              f'viewport {field} must be {kind} from {low} to {high}.')
    return dict(viewport)


def entry_params(browser, mode, owner, viewport=None):
    """Extra `auth.proxy_start` fields; the same-host wire request stays unchanged."""
    if browser in LOCAL_BROWSERS:
        return {}
    params = {'browser': browser, 'mode': mode, 'owner': owner}
    if viewport is not None:
        params['viewport'] = viewport
    return params


def entry_failure(remote, browser, mode):
    """Why a phone could not continue with GrantBridge's start response, or None."""
    if browser in LOCAL_BROWSERS:
        return None
    if remote.get('browser') != browser or remote.get('mode') != mode:
        return 'GrantBridge did not honour the requested browser location.'
    if mode == 'hosted':
        return None if 'viewerUrl' in remote else 'GrantBridge did not return a hosted browser page.'
    if not auth_contract.https_url(remote.get('authorizationUrl'), maximum=16384):
        return 'GrantBridge did not return an https authorization URL for the phone.'
    return None
