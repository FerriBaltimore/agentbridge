# GrantBridge proxy integration acceptance

An authorization URL proves only that the challenge started. A local
Management API response proves only local sidecar state. Neither establishes
live provider login or model entitlement.

## Current evidence

- AgentBridge deterministic tests cover durable login attempts, cancellation,
  identity binding, proxy route admission and safe persistence.
- A subprocess test exercises Python→GrantBridge Node adapter→fake CLIProxyAPI
  Management API. It uses no real account or token.
- The CLIProxyAPI lab covers Codex Responses routing and a tool loop with fake
  upstreams. It does not prove upstream OAuth or quota.
- Historical GrantBridge native provider tests and AgentBridge direct-provider
  acceptance predate v2. They do not certify the proxy account flow.

## Required live gate

For each of `codex`, `claude` and `grok`:

1. Start with an empty, dedicated sidecar and disposable data directories.
2. Run `accounts login` and complete OAuth in the supported same-host browser.
3. Observe one credential, identity and model catalogue; complete the login
   and reopen AgentBridge state, then query the CLIProxyAPI OAuth state again.
4. Submit an authenticated Codex turn through that sidecar, including a tool
   call and an explicit Stop test where supported.
5. Verify credential refresh, identity stability, model entitlement and any
   quota reading against actual provider behavior. Record unsupported or
   missing telemetry as unknown.
6. Exercise automatic account selection, a switch between turns, bounded
   portable context and refusal to change account within a turn.
7. Check that tokens, key values, raw provider errors and private reasoning
   are absent from argv, SQLite, logs, prompts and public events.

Record exact repository revisions, provider and runtime versions, account
identity, operations performed, observed evidence and omitted paths. A
provider-tested capability applies only to the matching deployment. Remote
browser and mobile acceptance is a separate gate; the current same-host flow
must not be presented as remote support.

## Deterministic and package checks

```bash
python tools/check_repository.py
python -m pytest
python -m pip wheel . --no-deps -w dist
```

Build and install the wheel in a disposable environment, run the installed
example and CLI, then reinstall delivered SDK changes into this repository's
`.venv`. GrantBridge's own tests and package checks run in its checkout. Tests
must never discover or use real accounts.
