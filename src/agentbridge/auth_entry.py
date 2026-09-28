"""Where the user completes a GrantBridge proxy login: this host or a phone.

`same_host` keeps the desktop path unchanged. A phone (`mobile`) opens the https
authorization URL itself (`mode='browser'`) or drives GrantBridge's hosted browser
(`mode='hosted'`). Contract: docs/interface/mobile-login.md.
"""

from . import auth_contract
from .errors import BridgeError

BROWSERS = ('same_host', 'mobile')
MODES = ('browser', 'hosted')


def validate_entry(mode, browser):
    if browser not in BROWSERS or mode not in MODES:
        raise BridgeError('invalid_request', 'browser must be same_host or mobile; mode must be browser or hosted.')
    if mode == 'hosted' and browser != 'mobile':
        raise BridgeError('unsupported_operation', 'A hosted browser is only offered to a mobile login.')


def entry_params(browser, mode, owner):
    """Extra `auth.proxy_start` fields; the same-host wire request stays unchanged."""
    return {} if browser == 'same_host' else {'browser': browser, 'mode': mode, 'owner': owner}


def entry_failure(remote, browser, mode):
    """Why a phone could not continue with GrantBridge's start response, or None."""
    if browser == 'same_host':
        return None
    if remote.get('browser') != browser or remote.get('mode') != mode:
        return 'GrantBridge did not honour the requested browser location.'
    if mode == 'hosted':
        return None if 'viewerUrl' in remote else 'GrantBridge did not return a hosted browser page.'
    if not auth_contract.https_url(remote.get('authorizationUrl'), maximum=16384):
        return 'GrantBridge did not return an https authorization URL for the phone.'
    return None
