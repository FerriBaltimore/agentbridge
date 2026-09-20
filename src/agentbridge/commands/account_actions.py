"""Execute account CLI commands through the SDK."""
from dataclasses import asdict, is_dataclass
from uuid import uuid4

from ..errors import BridgeError
from ..models import Account


def account_dict(account):
    return asdict(account) if is_dataclass(account) else account


def account_command(bridge, args):
    command = args.accounts_command
    if command == "add":
        account = Account(
            id=uuid4().hex,
            engine=args.engine,
            home=args.home,
            name=args.name,
            email=args.email,
            env_names=tuple(args.env_names or ()),
            key_env=args.key_env,
            command=tuple(args.command or ()),
        )
        return account_dict(bridge.register(account))
    if command == "list":
        return [account_dict(account) for account in bridge.accounts()]
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
                    print(f"Starting authentication for {args.name} ({args.engine})...")
                if attempt.get("authorization_url") or attempt.get("authorizationUrl"):
                    print("Open this URL to continue:")
                    print(f"  {attempt.get('authorization_url') or attempt.get('authorizationUrl')}")
                if attempt.get("user_code") or attempt.get("userCode"):
                    print(f"Verification code: {attempt.get('user_code') or attempt.get('userCode')}")
                print("Waiting for provider confirmation...")
            elif status == "exchanging":
                print("Provider confirmed the login. Verifying the native account...")

        result = bridge.account_login(
            engine=args.engine,
            name=args.name,
            email=args.email,
            grantbridge_root=args.grantbridge_root,
            data_dir=args.grantbridge_data_dir,
            mode=args.mode,
            browser=args.browser,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
            on_attempt=progress,
            inference=args.inference,
        )
        args.account_name = result["account"]["name"]
        return result
    if command == "login-start":
        return bridge.account_login_start(
            engine=args.engine, name=args.name, email=args.email,
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
            return bridge.account_login_check(**options, inference=args.inference)
        if command == "login-complete":
            return bridge.account_login_complete(**options)
        return bridge.account_login_cancel(**options)
    raise BridgeError("invalid_command", "Unknown accounts command.")
