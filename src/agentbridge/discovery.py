"""Capability and model discovery for the public SDK."""

from .capabilities import proxy_payload
from .errors import BridgeError, UnsupportedError
from .models import page_values


class DiscoveryMixin:
    def provider_contracts(self, engine=None):
        from .provider_contracts import ContractRegistry
        return ContractRegistry(self.store).list(engine)

    def provider_contract(self, contract_id):
        from .provider_contracts import ContractRegistry
        return ContractRegistry(self.store).get(contract_id)

    def provider_compatibility(self, account_ref):
        from .provider_contracts import ContractRegistry
        return ContractRegistry(self.store).check(self.resolve_account(account_ref))

    def provider_inspect(self, engine):
        from .provider_contracts import ContractRegistry
        return ContractRegistry(self.store).inspect(engine)

    def capabilities(self, engine=None, account_ref=None, refresh=False, include_parameters=True):
        if refresh:
            raise UnsupportedError('Capability refresh is not supported by this adapter.')
        if engine is not None:
            raise BridgeError('unsupported_parameter', 'The execution engine is fixed.')
        if account_ref:
            account = self.resolve_account(account_ref)
            if not account.proxy_base_url:
                raise BridgeError('invalid_proxy_account', 'This historical account cannot execute.')
        return proxy_payload(include_parameters=include_parameters)

    def models(self, engine=None, *, account_ref=None, refresh=False, include_hidden=False,
               include_deprecated=False, limit=None, cursor=0):
        if engine is not None:
            raise BridgeError('unsupported_parameter', 'Models are selected across proxy accounts.')
        if any(type(flag) is not bool for flag in (refresh, include_hidden, include_deprecated)):
            raise BridgeError('invalid_input', 'Catalog query flags must be booleans.')
        limit, cursor = page_values(limit, cursor, allow_none=True)
        result = self.routes.models(account_ref=account_ref, refresh=refresh)
        items = result['models']
        page = items[cursor:] if limit is None else items[cursor:cursor + limit]
        result['items'] = page
        result['models'] = page
        result['next_cursor'] = cursor + len(page) if cursor + len(page) < len(items) else None
        result['has_more'] = result['next_cursor'] is not None
        return result
