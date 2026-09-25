"""Prepare model-first sessions and account-pinned turns for the Bridge."""

from uuid import uuid4

from ..errors import BridgeError
from ..models import identifier, model_id
from ..native_sessions import require_native_session
from ..transports import require_proxy_account
from ..workspace_policy import validate_workspace


def normalized_exclusions(value):
    if (not isinstance(value, (tuple, list)) or len(value) > 1000
            or any(not isinstance(ref, str) or not 1 <= len(ref) <= 512
                   or not ref.strip()
                   or any(ord(char) < 32 or ord(char) == 127 for char in ref)
                   for ref in value)
            or len(set(value)) != len(value)):
        raise BridgeError('invalid_request', 'Excluded account references must be bounded and unique.')
    return tuple(value)


def verify_proxy_model(routes, account, model, *, refresh, context_window=None):
    require_proxy_account(account)
    if not account.management_key_env:
        raise BridgeError('proxy_binding_unverified', 'This proxy account needs a management key reference.')
    if model is None:
        raise BridgeError('model_required', 'Choose a model for proxy routing.')
    model_id(model)
    if model not in account.supported_models:
        raise BridgeError('model_unavailable', 'The proxy account does not declare this model.')
    observed = routes.observation(account, refresh=refresh)
    if not routes._verified(account, observed):
        raise BridgeError('proxy_binding_unverified', 'The proxy account binding is unverified.')
    if model not in {item.get('id') for item in observed['data']['models']
                     if isinstance(item, dict)}:
        raise BridgeError('model_unavailable', 'The proxy does not currently expose this model.')
    if context_window is not None:
        ceiling = routes.context_ceiling(account, model)
        if ceiling is None:
            raise BridgeError('context_window_unavailable',
                              'The selected account has no verified context window ceiling.')
        if context_window > ceiling:
            raise BridgeError('context_window_unavailable',
                              'The requested context window exceeds the selected account ceiling.')


def create_automatic_instance(bridge, *, workspace_path, model, provider=None,
                              idempotency_key, evaluation=False, excluded_account_refs=(),
                              initial_account=None):
    """Select an initial route while keeping the conversation automatically routed."""
    if model is None:
        raise BridgeError('model_required', 'Choose a model for automatic routing.')
    model_id(model)
    if provider is not None:
        identifier(provider)
    cwd = str(validate_workspace(workspace_path or '.', bridge.store.root))
    replayed = bridge.store.replay_auto_session(idempotency_key, cwd, model, provider=provider,
                                                evaluation=evaluation,
                                                excluded_account_refs=excluded_account_refs,
                                                initial_account_id=initial_account.id if initial_account else None)
    if replayed:
        return {**bridge._public_instance(bridge.get_session(replayed)), 'replayed': True}
    if initial_account is None:
        decision = bridge.routes.select(model, provider=provider,
                                        excluded_account_refs=excluded_account_refs)
        account_id = decision.account_id
    else:
        account_id = initial_account.id
        if bridge.store.retirement_status(account_id)['retired']:
            raise BridgeError('account_removed', 'The selected proxy account has been removed.')
        excluded = {bridge.resolve_account(ref).id for ref in excluded_account_refs}
        if account_id in excluded:
            raise BridgeError('invalid_request', 'The initial account is excluded from routing.')
        if provider is not None and provider != initial_account.provider:
            raise BridgeError('provider_unavailable', 'The initial account does not match the provider filter.')
        if bridge.store.pause_status(account_id)['paused']:
            raise BridgeError('account_paused', 'The selected proxy account is paused for new work.')
        verify_proxy_model(bridge.routes, initial_account, model, refresh=True)
    session_id, created = bridge.store.add_session(
        uuid4().hex, account_id, cwd, model,
        request_key=idempotency_key, routing_mode='automatic', routing_provider=provider,
        evaluation=evaluation, excluded_account_refs=excluded_account_refs,
        initial_account_id=initial_account.id if initial_account else None)
    return {**bridge._public_instance(bridge.get_session(session_id)), 'replayed': not created}


def prepare_turn(bridge, session, options, *, excluded_account_refs=()):
    """Select a proxy route without replacing the instance's Codex history."""
    with bridge.store.connect() as db:
        require_native_session(db, session)
    routing = bridge.store.routing(session['id'])
    if routing['mode'] != 'automatic':
        if excluded_account_refs:
            raise BridgeError('invalid_request', 'Pinned accounts cannot use route exclusions.')
        account = bridge.account(session['account_id'])
        if bridge.store.pause_status(account.id)['paused']:
            raise BridgeError('account_paused', 'The selected proxy account is paused for new work.')
        verify_proxy_model(bridge.routes, account, options.model or session['model'],
                           refresh=True, context_window=options.context_window)
        return account, None, None, 0, None
    model = options.model or session['model']
    if model is None:
        raise BridgeError('model_required', 'Choose a model for automatic routing.')
    decision = bridge.routes.select(model, provider=routing['provider'],
                                    excluded_account_refs=excluded_account_refs,
                                    context_window=options.context_window,
                                    affinity_account_id=routing.get('affinity_account_id'))
    account = bridge.account(decision.account_id)
    verify_proxy_model(bridge.routes, account, model, refresh=False,
                       context_window=options.context_window)
    return account, decision, None, 0, None
