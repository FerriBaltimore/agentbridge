"""Render safe account results for a human terminal."""
import json
from .usage_output import print_usage


def _provider(value):
    return value.get('provider') or '-'


def _models(value):
    return ', '.join(value.get('supported_models') or ()) or '-'


def print_account_human(command, value, account_name=None):
    if command == "list":
        if not value:
            print("No accounts registered.")
            return
        print("Name | Provider | Models | Email")
        print("---|---|---|---")
        for account in value:
            print(" | ".join((str(account.get('name') or '-'), _provider(account),
                              _models(account), str(account.get('email') or '-'))))
        return
    if command == "delete":
        print(f"Account removed from AgentBridge: {value['account_ref']}")
        if value.get('upstream_credential_removed') is False:
            print("The CLIProxyAPI OAuth credential remains in the local proxy.")
        return
    if command == "status":
        auth = value.get("authentication", {})
        identity = value.get("identity") or {}
        configured = value.get("configured") or {}
        print(f"Account: {account_name or configured.get('name') or value.get('account_id')}")
        print(f"Provider: {_provider(configured)}")
        print(f"Models: {_models(configured)}")
        print(f"Configured name: {configured.get('name') or '-'}")
        print(f"Configured email: {configured.get('email') or '-'}")
        print(f"Authentication: {auth.get('status') or '-'}")
        print(f"Observed identity: {identity.get('email') or identity.get('type') or '-'}")
        print(f"Observed at: {auth.get('observed_at') or '-'}")
        if value.get("reason"):
            print(f"Reason: {value['reason']}")
        return
    if command == "usage":
        print_usage(value, account_name)
        return
    if command == "reset-credits":
        print(f"Account: {account_name or value.get('account_ref') or '-'}")
        print(f"Reset credits: {value.get('available_count') if value.get('available_count') is not None else 'unknown'}")
        print(f"Status: {value.get('status') or 'unknown'}")
        print(f"Observed at: {value.get('observed_at') or 'unknown'}")
        print(f"Stale: {'yes' if value.get('stale', True) else 'no'}")
        if value.get('observation_ref'):
            print(f"Observation reference: {value['observation_ref']}")
        if value.get('reason'):
            print(f"Reason: {value['reason']}")
        if value.get('pending_reset'):
            print("Pending redemption: inspect the existing attempt before retrying with its key.")
        for credit in value.get('credits') or []:
            print(f"Credit: {credit.get('id') or '-'} ({credit.get('status') or 'unknown'})")
            if credit.get('expires_at'):
                print(f"  Expires at: {credit['expires_at']}")
        return
    if command == "quota-reset":
        print(f"Account: {account_name or value.get('account_ref') or '-'}")
        print(f"Redemption outcome: {value.get('outcome') or 'unknown'}")
        if value.get('windows_reset') is not None:
            print(f"Windows reset: {value['windows_reset']}")
        credits = value.get('reset_credits') or {}
        if credits:
            print(f"Remaining reset credits: {credits.get('available_count') if credits.get('available_count') is not None else 'unknown'}")
        return
    if command == "history":
        print(f"Account: {account_name or '-'}")
        if not value:
            print("No usage observations.")
            return
        print("Observed at | Source | Stale")
        print("---|---|---")
        for observation in value:
            print(" | ".join(str(observation.get(key)) for key in ("observed_at", "source", "stale")))
            print_usage(observation.get('data', {}), account_name)
        return
    if command == "check":
        print(f"Account: {account_name or '-'}")
        print("Status:")
        print_account_human("status", value.get("status", {}), account_name)
        print("Usage:")
        print_account_human("usage", value.get("usage", {}), account_name)
        return
    if command == "login":
        account = value.get("account") or {}
        print(f"Account authenticated: {account.get('name') or account.get('id') or '-'} ({_provider(account)})")
        if value.get("identity", {}).get("email"):
            print(f"Observed identity: {value['identity']['email']}")
        print("Authentication: authenticated")
        return
    if command in {"login-start", "login-status", "login-check", "login-complete", "login-cancel"}:
        print(f"Authentication attempt: {value.get('id') or value.get('attempt_id') or '-'}")
        print(f"Status: {value.get('status') or '-'}")
        if value.get('authorization_url') or value.get('authorizationUrl'):
            print(f"Open this URL: {value.get('authorization_url') or value.get('authorizationUrl')}")
        if value.get('user_code') or value.get('userCode'):
            print(f"Verification code: {value.get('user_code') or value.get('userCode')}")
        return
    print(json.dumps(value, indent=2, ensure_ascii=False))
