"""Render safe account results for a human terminal."""
import json
from .usage_output import print_usage


def print_account_human(command, value, account_name=None):
    if command == "list":
        if not value:
            print("No accounts registered.")
            return
        print("Name | Engine | Email")
        print("---|---|---")
        for account in value:
            print(" | ".join(str(account.get(key) or "-") for key in ("name", "engine", "email")))
        return
    if command == "add":
        print(f"Account added: {value['name']} ({value['engine']})")
        return
    if command == "status":
        auth = value.get("authentication", {})
        identity = value.get("identity") or {}
        configured = value.get("configured") or {}
        print(f"Account: {account_name or configured.get('name') or value.get('account_id')}")
        print(f"Engine: {configured.get('engine') or '-'}")
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
        print(f"Account authenticated: {account.get('name') or account.get('id') or '-'} ({account.get('engine') or '-'})")
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
