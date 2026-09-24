"""Validate and admit a complete turn against one verified account route."""

from dataclasses import replace
import os
from uuid import uuid4

from ..credentials import environment
from ..errors import BridgeError, BusyError
from ..provider_contracts import ContractRegistry
from ..security import Redactor
from ..transports import command
from .admission import prepare_turn


def admit_turn(bridge, session, prompt, options, request_key, message_id,
               excluded_account_refs=()):
    """Retry a pre-execution account collision against another eligible route."""
    model = options.model or session['model']
    candidates = sum(1 for item in bridge.accounts()
                     if item.proxy_base_url and model in item.supported_models)
    attempts = min(8, max(1, candidates))
    for attempt in range(attempts):
        account, decision, context, omissions, event_seq = prepare_turn(
            bridge, session, options, excluded_account_refs=excluded_account_refs)
        if account.proxy_base_url and model not in account.supported_models:
            raise BridgeError('model_unavailable', 'The selected account does not declare this model.')
        command(account, session, options)
        contracts = ContractRegistry(bridge.store)
        receipt = contracts.check(account, enforce=True)
        secrets = environment(account)
        management_key = os.environ.get(account.management_key_env or '')
        if not management_key:
            raise BridgeError('credential_unavailable', 'The proxy management credential is unavailable.')
        secrets[account.management_key_env] = management_key
        redactor = Redactor(secrets.values())
        clean_prompt = redactor.clean(prompt)
        clean_options = replace(options, attachments=tuple(
            {key: redactor.clean(value) if key in {'name', 'text'} else value
             for key, value in item.items()} for item in options.attachments))
        try:
            run_id, created = bridge.store.admit(
                uuid4().hex, session['id'], clean_prompt, clean_options, request_key,
                message_id=message_id or uuid4().hex,
                account_id=account.id if decision else None,
                route_decision=decision, route_context=context,
                route_omissions=omissions, route_event_seq=event_seq,
                excluded_account_refs=excluded_account_refs)
            return run_id, created, receipt, secrets
        except BusyError:
            if decision is None or attempt + 1 == attempts:
                raise
    raise AssertionError('Admission attempts exhausted without a result.')
