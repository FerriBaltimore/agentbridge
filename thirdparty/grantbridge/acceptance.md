# Acceptance plan

The integration is complete only when deterministic suites and real provider
checks agree. A URL being displayed is evidence that a login challenge started,
not evidence that credentials can be used.

## Existing evidence

GrantBridge currently has:

- deterministic OAuth, OIDC and MCP tests;
- encrypted storage and refresh rotation tests;
- owner, callback, cancellation and permission checks;
- `npm run test:real`, which starts the installed Claude, Codex and Cursor
  adapters and verifies their real authorization URLs without completing
  consent.

AgentBridge currently has:

- deterministic worker, protocol, account, stop and recovery tests;
- a real Codex account and rate-limit reader;
- account references that do not store credential values.

These are separate suites. They do not yet prove the cross-repository
login-to-run flow.

## Required integration tests

1. Start a GrantBridge sidecar from AgentBridge and perform a controlled
   fake-provider login.
2. Register or activate the returned account reference in AgentBridge.
3. Run fake Codex, Claude and Cursor workers using that reference.
4. Verify that no credential appears in argv, SQLite, prompts, logs or events.
5. Reopen both stores and repeat a fresh account check.
6. Cancel an in-flight authentication and verify that no account is replaced.
7. Reauthenticate an existing account as the same identity and verify an atomic
   credential-generation replacement.
8. Try a different provider identity and verify that AgentBridge refuses the
   update while the old account remains usable.
9. Interrupt the sidecar and worker independently and verify explicit recovery
   states.
10. Run the hosted-browser flow from a phone or emulator and verify that the
    phone sees the provider page while the server retains the browser profile
    and credential.
11. Complete a real Claude, Codex and Cursor login in isolated test accounts,
    then perform a fresh-process check, an authenticated operation and a
    reconnect after restarting the host.
12. Complete Google and one configurable OAuth/OIDC flow, including refresh,
    refresh-token rotation, revocation and reauthorization.

## Commands

Run builds and tests through the workspace's managed temporary job wrapper.
The exact checkout paths are local configuration, not part of the contract:

```bash
# AgentBridge
python -m pytest
python -m pip wheel . --no-deps -w dist

# GrantBridge, from its checkout
npm ci
npm test
npm run lint
npm run test:real
```

The live checks must use disposable data directories and accounts. They must
record provider versions, runtime versions, account identity, the operation
performed and what was not tested. Never discover or reuse a developer's
default account from a test.
