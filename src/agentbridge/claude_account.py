"""Claude profile identity and explicit account quota refresh, without inference."""
import json
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

from .credentials import environment
from .errors import BridgeError
from .security import base_environment
from .quota_windows import normalize, number


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def identity(account):
    env = base_environment()
    env.update(environment(account))
    env['CLAUDE_CONFIG_DIR'] = account.home
    try:
        result = subprocess.run(list(account.command or ('claude',)) + ['auth', 'status'],
                                cwd=account.home, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=15)
        if len(result.stdout) > 65536:
            raise ValueError()
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError()
        email = value.get('email')
        if result.returncode or not value.get('loggedIn') or not isinstance(email, str):
            return {'status': 'authentication_required', 'identity': {}, 'live_request': False}
        if account.email and account.email != email:
            return {'status': 'identity_changed', 'identity': {}, 'live_request': False}
        return {'status': 'loaded_only', 'identity': {'email': email}, 'live_request': False,
                'reason': 'native_profile_loaded'}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise BridgeError('provider_unavailable', 'Claude account status is unavailable.') from None


def quota(account):
    # Only the explicitly bound native profile is consulted; no ambient login discovery.
    path = Path(account.home) / '.credentials.json'
    try:
        with path.open('rb') as file:
            raw = file.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        value = json.loads(raw).get('claudeAiOauth') or {}
        token = value.get('accessToken')
        if not isinstance(token, str) or not token:
            raise ValueError()
    except (OSError, ValueError, AttributeError):
        raise BridgeError('credential_unavailable', 'The bound Claude profile has no OAuth credential.') from None
    return quota_request(token)


def quota_request(token):
    """One bounded, no-redirect request shared by the SDK compatibility surface."""
    request = urllib.request.Request('https://api.anthropic.com/api/oauth/usage', headers={
        'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20', 'Accept': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
    except urllib.error.HTTPError as error:
        code = 'authentication_required' if error.code in {401, 403} else 'rate_limited' if error.code == 429 else 'provider_unavailable'
        raise BridgeError(code, 'Claude did not provide a quota observation.') from None
    except (OSError, ValueError):
        raise BridgeError('provider_unavailable', 'Claude did not provide a quota observation.') from None
    windows = [row for row in normalize('claude', value) if row['used_percent'] is not None]
    extra = value.get('extra_usage')
    extra = extra if isinstance(extra, dict) else {}
    spend = {'enabled': extra.get('is_enabled') if type(extra.get('is_enabled')) is bool else None,
             'monthly_limit': number(extra.get('monthly_limit')), 'used': number(extra.get('used_credits')),
             'used_percent': number(extra.get('utilization')), 'unit': 'provider_credits',
             'currency': extra.get('currency') if isinstance(extra.get('currency'), str)
                         and len(extra['currency']) == 3 and extra['currency'].isalpha() else None}
    return {'windows': windows, 'reason': None if windows else 'quota_not_reported',
            'source': 'claude_oauth_usage', 'scope': 'account', 'supported': True,
            'stale': False, 'provider_contract': 'native_oauth_compatibility', 'extra_usage': spend}
