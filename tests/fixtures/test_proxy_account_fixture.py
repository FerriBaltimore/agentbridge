"""Verified proxy account setup for store tests without real credentials or network."""

from hashlib import sha256
from dataclasses import replace
import json
import time

from agentbridge import Account
from agentbridge.proxy import ManagementClient, ProxyRoute


def proxy_account(account_id, port, *, model="fixture-model", provider="fixture"):
    return Account(
        account_id,
        "codex",
        provider=provider,
        supported_models=(model,),
        proxy_base_url=f"http://127.0.0.1:{port}/v1",
        key_env="FIXTURE_PROXY_KEY",
        management_key_env="FIXTURE_MANAGEMENT_KEY",
    )


def register_verified_proxy_account(store, account_id, port, *, model="fixture-model",
                                    provider="fixture"):
    """Seed the same bounded, fresh evidence required by proxy-only admission."""
    account = proxy_account(account_id, port, model=model, provider=provider)
    return seed_authenticated_proxy_account(store, account)


def seed_authenticated_proxy_account(store, account, *, observe_local=False,
                                     record_observation=True):
    """Seed a completed login in fixture SQLite, outside the product API."""
    account = replace(account, name=account.name or account.id)
    observed = (ManagementClient(
        ProxyRoute(account.id, account.proxy_base_url, account.key_env),
        account.management_key_env).observe() if observe_local else None)
    binding = (observed['binding_fingerprint'] if observed else
               sha256(f"fixture-binding:{account.id}".encode()).hexdigest())
    identity = (observed['identity_fingerprint'] if observed else
                sha256(f"fixture-identity:{account.id}".encode()).hexdigest())
    route = {key: getattr(account, key) for key in
             ('proxy_base_url', 'key_env', 'management_key_env')}
    attempt_id = f"fixture-login-{account.id}"
    now = time.time()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO accounts(id,config) VALUES (?,?)",
                   (account.id, json.dumps(account.to_dict())))
        db.execute('''INSERT INTO auth_attempts
            (id,owner,account_id,engine,name,email,mode,browser,grantbridge_id,
             status,data,created,updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (attempt_id, "fixture-owner", account.id, account.provider, account.name,
             account.email, "browser", "same_host", f"fixture-state-{account.id}",
             "bound", json.dumps({"state": "usable"}), now, now))
        db.execute("INSERT INTO auth_proxy_routes(attempt_id,config,connection) VALUES (?,?,?)",
                   (attempt_id, json.dumps(route), "{}"))
        db.execute("INSERT INTO proxy_bindings VALUES (?,?,?)",
                   (account.id, binding, identity))
    if record_observation:
        data = ({key: value for key, value in observed.items()
                 if key not in {'binding_fingerprint', 'identity_fingerprint', 'email'}}
                if observed else {
                    "account_id": account.id, "provider": account.provider,
                    "status": "active", "disabled": False, "unavailable": False,
                    "models": [{"id": model, "used_percent": None}
                               for model in account.supported_models],
                    "model_metadata": {},
                })
        store.account_observation(account.id, "cliproxy_management", "active",
                                  {**data, "binding_verified": True})
    return account
