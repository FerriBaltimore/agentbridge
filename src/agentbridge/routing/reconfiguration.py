"""Validate a requested route against durable, login-bound proxy accounts."""

import json

from ..errors import BridgeError
from ..models import Account, model_id
from ..native_sessions import require_native_session
from .binding import has_bound_proxy_login


PROVIDER_UNSET = object()
SUPPORTED_PROVIDERS = frozenset({'codex', 'claude', 'grok'})


def validate_mode(mode):
    if not isinstance(mode, str) or mode not in {'pinned', 'automatic'}:
        raise BridgeError('invalid_routing_mode', 'routing_mode must be pinned or automatic.')


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


def configure_route(db, session, model, *, mode=PROVIDER_UNSET,
                    provider=PROVIDER_UNSET, account_id=PROVIDER_UNSET):
    """Change policy atomically while preserving native-session ownership."""
    require_native_session(db, session)
    routing = db.execute('SELECT * FROM session_routing WHERE session_id=?',
                         (session['id'],)).fetchone()
    if routing is None:
        raise BridgeError('schema_version', 'Session routing metadata is missing.')
    if mode is PROVIDER_UNSET:
        mode = ('pinned' if account_id is not PROVIDER_UNSET else
                'automatic' if provider is not PROVIDER_UNSET else routing['mode'])
    current_account = (routing['affinity_account_id'] if routing['mode'] == 'automatic'
                       else None) or session['account_id']
    chosen = current_account if account_id is PROVIDER_UNSET else account_id
    selected_provider = routing['provider'] if provider is PROVIDER_UNSET else provider
    if mode == 'pinned':
        if provider is not PROVIDER_UNSET and provider is not None:
            raise BridgeError('unsupported_parameter', 'provider filters automatic routing only.')
        selected_provider = None
    require_declared_route(db, model, provider=selected_provider,
                           account_id=chosen if mode == 'pinned' or account_id is not PROVIDER_UNSET else None)
    db.execute('UPDATE session_routing SET mode=?,provider=?,affinity_account_id=? WHERE session_id=?',
               (mode, selected_provider, chosen if mode == 'automatic' else None, session['id']))
    if (mode == 'pinned' or account_id is not PROVIDER_UNSET) and chosen != session['account_id']:
        # The proxy account is an upstream route; Codex owns the conversation.
        return {'account_id': chosen}
    return {}
