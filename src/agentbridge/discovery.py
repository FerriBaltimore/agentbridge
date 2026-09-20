"""Capability and model discovery for the public SDK."""

from .capabilities import payload as capability_payload
from .errors import BridgeError, UnsupportedError
from .models import CAPABILITIES, page_values


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
        if account_ref:
            account = self.resolve_account(account_ref)
            if engine and account.engine != engine:
                raise BridgeError('invalid_engine', 'The account engine does not match the requested engine.')
            engine = account.engine
        if engine is not None and engine not in CAPABILITIES:
            raise BridgeError('invalid_engine', 'Unknown engine.')
        value = capability_payload(engine)
        if include_parameters:
            return value
        if engine is None:
            return {name: {key: item for key, item in data.items() if key != 'parameters'}
                    for name, data in value.items()}
        return {key: item for key, item in value.items() if key != 'parameters'}

    def models(self, engine, *, account_ref=None, refresh=False, include_hidden=False,
               include_deprecated=False, limit=None, cursor=0):
        limit, cursor = page_values(limit, cursor, allow_none=True)
        result = self.catalog.list(engine, account_ref=account_ref, refresh=refresh,
                                   include_hidden=include_hidden, include_deprecated=include_deprecated)
        items = result['models']
        page = items[cursor:] if limit is None else items[cursor:cursor + limit]
        result['items'] = page
        result['models'] = page
        result['next_cursor'] = cursor + len(page) if cursor + len(page) < len(items) else None
        result['has_more'] = result['next_cursor'] is not None
        return result
