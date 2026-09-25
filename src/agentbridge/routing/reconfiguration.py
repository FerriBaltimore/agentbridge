"""Validate a requested route against durable, login-bound proxy accounts."""

import json

from ..errors import BridgeError
from ..models import Account, model_id
from .binding import has_bound_proxy_login


PROVIDER_UNSET = object()
SUPPORTED_PROVIDERS = frozenset({'codex', 'claude', 'grok'})


def validate_provider(provider):
    if provider is not None and (not isinstance(provider, str)
                                 or provider not in SUPPORTED_PROVIDERS):
        raise BridgeError('invalid_provider', 'Choose a supported proxy provider.')


def require_declared_route(db, model, *, provider=None, account_id=None):
    """Check stored eligibility; live catalogue checks happen at turn admission."""
    if model is None:
        raise BridgeError('model_required', 'Choose a model for proxy routing.')
    model_id(model)
    rows = db.execute('SELECT id,config FROM accounts ORDER BY id')
    for row in rows:
        if account_id is not None and row['id'] != account_id:
            continue
        if db.execute('SELECT 1 FROM paused_accounts WHERE account_id=?',
                      (row['id'],)).fetchone():
            continue
        try:
            account = Account(**json.loads(row['config']))
        except (BridgeError, TypeError, ValueError):
            continue
        if (account.id != row['id'] or account.engine != 'codex'
                or not account.proxy_base_url or not account.management_key_env
                or model not in account.supported_models
                or (provider is not None and account.provider != provider)):
            continue
        if not has_bound_proxy_login(db, account):
            continue
        if db.execute('SELECT 1 FROM proxy_bindings WHERE account_id=?',
                      (account.id,)).fetchone():
            return
    raise BridgeError('model_unavailable',
                      'No eligible proxy account declares support for this model and provider.')
