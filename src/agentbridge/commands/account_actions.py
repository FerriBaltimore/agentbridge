"""Execute account CLI commands through the SDK."""
from dataclasses import asdict, is_dataclass

from ..errors import BridgeError


def account_dict(account):
    return asdict(account) if is_dataclass(account) else account


def account_command(bridge, args):
    command = args.accounts_command
    if command == "list":
        return [account_dict(account) for account in bridge.accounts()]
    if command == "delete":
        if (args.name is None) == (args.account_id is None):
            raise BridgeError('invalid_request',
                              'Provide either an account reference or --account-id.')
        return (bridge.account_delete(account_id=args.account_id) if args.account_id
                else bridge.account_delete(args.name))
    if command in {"status", "usage", "history", "check"}:
        account = bridge.resolve_account(args.name)
        args.account_name, args.id = account.name, account.id
        if command == "status":
            return bridge.account_status(account.id, refresh=args.refresh)
        if command == "usage":
            return bridge.account_usage(account.id, refresh=args.refresh)
        if command == "history":
            return bridge.account_usage_history(account.id, limit=args.limit)
        return {
            "status": bridge.account_status(account.id, refresh=True),
            "usage": bridge.account_usage(account.id, refresh=True),
        }
    if command == "login":
        seen = set()

        def progress(attempt):
            if args.json or not isinstance(attempt, dict):
                return
            status = attempt.get("status")
            if status in seen:
                return
            seen.add(status)
            if status in ("starting", "awaiting_user"):
                if status == "starting":
                    print(f"Starting authentication for {args.name} ({args.provider})...")
                if attempt.get("authorization_url") or attempt.get("authorizationUrl"):
                    print("Open this URL to continue:")
                    print(f"  {attempt.get('authorization_url') or attempt.get('authorizationUrl')}")
                if attempt.get("user_code") or attempt.get("userCode"):
                    print(f"Verification code: {attempt.get('user_code') or attempt.get('userCode')}")
                print("Waiting for provider confirmation...")
            elif status == "exchanging":
                print("Provider confirmed the login. Verifying the proxy account...")

        result = bridge.account_login(
            provider=args.provider,
            name=args.name,
            proxy_base_url=args.proxy_base_url,
            key_env=args.key_env,
            management_key_env=args.management_key_env,
            email=args.email,
            grantbridge_root=args.grantbridge_root,
            data_dir=args.grantbridge_data_dir,
            mode=args.mode,
            browser=args.browser,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            on_attempt=progress,
        )
        args.account_name = result["account"]["name"]
        return result
    if command == "login-start":
        return bridge.account_login_start(
            provider=args.provider, name=args.name,
            proxy_base_url=args.proxy_base_url, key_env=args.key_env,
            management_key_env=args.management_key_env, email=args.email,
            grantbridge_root=args.grantbridge_root, data_dir=args.grantbridge_data_dir,
            mode=args.mode, browser=args.browser, request_key=args.request_key,
            owner_ref=args.owner_ref)
    if command in {"login-status", "login-check", "login-complete", "login-cancel"}:
        options = {
            "attempt_id": args.attempt_id,
            "owner_ref": args.owner_ref,
            "account_ref": args.account_ref,
            "grantbridge_root": args.grantbridge_root,
            "data_dir": args.grantbridge_data_dir,
        }
        if command == "login-status":
            return bridge.account_login_status(**options)
        if command == "login-check":
            return bridge.account_login_check(**options)
        if command == "login-complete":
            return bridge.account_login_complete(**options)
        return bridge.account_login_cancel(**options)
    raise BridgeError("invalid_command", "Unknown accounts command.")
