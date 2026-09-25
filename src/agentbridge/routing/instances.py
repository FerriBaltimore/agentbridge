"""Public instance routing choices, independent of provider-native sessions."""

from ..errors import BridgeError, UnsupportedError
from .admission import create_automatic_instance, normalized_exclusions
from .reconfiguration import PROVIDER_UNSET, validate_mode, validate_provider


class InstanceRoutingMixin:
    def instance_create(self, *, engine=None, account_ref=None, provider=None,
                        routing_mode=None, workspace_path=None, model=None,
                        effort=None, context_window=None, permission_mode='dontAsk',
                        sandbox_mode='read-only', allowed_tools=(), metadata=None,
                        provider_options=None, continuity_mode=None, idempotency_key=None,
                        evaluation=False, excluded_account_refs=()):
        excluded_account_refs = normalized_exclusions(excluded_account_refs)
        if type(evaluation) is not bool:
            raise BridgeError('invalid_request', 'evaluation must be a boolean.')
        if provider_options or metadata or continuity_mode:
            raise UnsupportedError('Provider-specific options, metadata and continuity require an adapter contract.')
        if effort or context_window or permission_mode != 'dontAsk' or sandbox_mode != 'read-only' or allowed_tools:
            raise UnsupportedError('Instance defaults are configured per turn by this adapter.')
        if engine is not None:
            raise BridgeError('unsupported_parameter', 'Execution always uses the routed Codex engine.')
        if routing_mode is not None:
            validate_mode(routing_mode)
        mode = routing_mode or ('pinned' if account_ref is not None else 'automatic')
        validate_provider(provider)
        if mode == 'pinned':
            if provider is not None:
                raise BridgeError('unsupported_parameter', 'provider filters automatic routing only.')
            if excluded_account_refs:
                raise BridgeError('invalid_request', 'Pinned accounts cannot use route exclusions.')
            if account_ref is None:
                raise BridgeError('account_ref_required', 'Pinned routing requires an account_ref.')
            account = self.account_service.resolve(account_ref, include_retired=True)
            result = self.session(account.id, workspace_path or '.', model=model,
                                  request_key=idempotency_key, evaluation=evaluation)
            return self._public_instance(result)
        initial_account = (self.account_service.resolve(account_ref, include_retired=True)
                           if account_ref is not None else None)
        return create_automatic_instance(
            self, workspace_path=workspace_path, model=model, provider=provider,
            idempotency_key=idempotency_key, evaluation=evaluation,
            excluded_account_refs=excluded_account_refs, initial_account=initial_account)

    def instance_update(self, instance_id, *, model=None, provider=PROVIDER_UNSET,
                        routing_mode=PROVIDER_UNSET, account_ref=PROVIDER_UNSET,
                        effort=None, context_window=None,
                        permission_mode=None, sandbox_mode=None, allowed_tools=None,
                        expected_version=None, metadata=None, provider_options=None, state=None):
        if any(value is not None for value in (effort, context_window, permission_mode,
                                               sandbox_mode, allowed_tools, metadata, provider_options)):
            raise UnsupportedError('Only model, provider, routing_mode, account_ref and state updates are supported.')
        values = {}
        if model is not None:
            values['model'] = model
        if state is not None:
            values['state'] = state
        if not values and all(value is PROVIDER_UNSET for value in
                              (provider, routing_mode, account_ref)):
            raise BridgeError('invalid_request', 'At least one instance field must change.')
        account_id = PROVIDER_UNSET
        if account_ref is not PROVIDER_UNSET:
            if not isinstance(account_ref, str) or not account_ref.strip():
                raise BridgeError('invalid_request', 'account_ref must identify an account.')
            account_id = self.resolve_account(account_ref).id
        updated = self.store.update_session(
            instance_id, expected_version=expected_version, routing_provider=provider,
            routing_mode=routing_mode, routing_account_id=account_id, **values)
        return self._public_instance(updated)
