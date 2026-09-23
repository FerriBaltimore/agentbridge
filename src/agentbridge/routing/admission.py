"""Prepare model-first sessions and account-pinned turns for the Bridge."""

from pathlib import Path
from uuid import uuid4

from ..continuity import build
from ..errors import BridgeError
from ..models import identifier, model_id
from ..transports import require_proxy_account


def verify_proxy_model(routes, account, model, *, refresh):
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


def create_automatic_instance(bridge, *, workspace_path, model, provider=None,
                              idempotency_key):
    """Select an initial route while keeping the conversation automatically routed."""
    if model is None:
        raise BridgeError('model_required', 'Choose a model for automatic routing.')
    model_id(model)
    if provider is not None:
        identifier(provider)
    cwd = str(Path(workspace_path or '.').expanduser().resolve())
    if not Path(cwd).is_dir():
        raise BridgeError('invalid_workspace', 'Workspace must be an existing directory.')
    replayed = bridge.store.replay_auto_session(idempotency_key, cwd, model, provider=provider)
    if replayed:
        return {**bridge._public_instance(bridge.get_session(replayed)), 'replayed': True}
    decision = bridge.routes.select(model, provider=provider)
    session_id, created = bridge.store.add_session(
        uuid4().hex, decision.account_id, cwd, model,
        request_key=idempotency_key, routing_mode='automatic', routing_provider=provider)
    return {**bridge._public_instance(bridge.get_session(session_id)), 'replayed': not created}


def prepare_turn(bridge, session, options):
    """Return the account, decision and bounded context for one whole turn."""
    routing = bridge.store.routing(session['id'])
    if routing['mode'] != 'automatic':
        account = bridge.account(session['account_id'])
        verify_proxy_model(bridge.routes, account, options.model or session['model'], refresh=True)
        return account, None, None, 0, None
    model = options.model or session['model']
    if model is None:
        raise BridgeError('model_required', 'Choose a model for automatic routing.')
    decision = bridge.routes.select(model, provider=routing['provider'])
    account = bridge.account(decision.account_id)
    verify_proxy_model(bridge.routes, account, model, refresh=False)
    previous = routing['last_completed_account_id']
    prior = bridge.store.last_session_run(session['id'])
    account_changed = (prior is not None and prior['account_id'] != account.id) or (
        previous is not None and previous != account.id)
    needs_portable_context = prior is not None and (
        account_changed or not routing['last_native_id'] or prior['state'] != 'completed')
    if not needs_portable_context:
        return account, decision, None, 0, None
    for _ in range(3):
        before = bridge.store.last_route_event_seq(session['id'])
        bundle = build(bridge.store, session['id'], budget_bytes=128000)
        after = bridge.store.last_route_event_seq(session['id'])
        if before == after:
            return account, decision, bundle.text, len(bundle.omitted), after
    raise BridgeError('context_stale', 'Conversation evidence changed while preparing portable context.',
                      retryable=True)
