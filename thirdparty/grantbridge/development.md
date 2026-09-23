# Development workflow for account login

AgentBridge v2 has one account onboarding command. Prepare an empty, dedicated
CLIProxyAPI sidecar on loopback, with one auth directory and client and
management keys supplied through environment variables. This prepares
infrastructure only; it does not create an AgentBridge account.

```bash
agentbridge accounts login --provider codex --name "Development Codex" \
  --proxy-base-url http://127.0.0.1:8317/v1 \
  --proxy-key-env DEVELOPMENT_PROXY_KEY \
  --proxy-management-key-env DEVELOPMENT_MANAGEMENT_KEY
```

Use `login-start` and `login-status/check/complete/cancel` when the caller
needs restart-safe asynchronous control. The blocking command uses the same
state machine. GrantBridge starts provider OAuth through CLIProxyAPI's
Management API and reports a safe URL or challenge state. The browser and
sidecar currently need to share a host. CLIProxyAPI stores the resulting
credential; AgentBridge creates the account only after a fresh one-credential
identity and model check. An interrupted or failed attempt is never silently
restarted. A changed identity is not promoted.

The management key value travels only through trusted local stdio and local
Management API requests. Public API parameters and account records contain
its environment variable name, not its value. Both automatic and pinned
routes require it. The proxy client key is supplied to Codex at execution;
the management key is not.

## Remote browser work

A phone opening a loopback authorization callback would return to the phone,
not the sidecar host. A remote deployment needs an authenticated hosted browser
or a provider-supported server callback and a separate acceptance run. The
current same-host browser mode does not imply that those routes work. The
remote viewer must never send upstream credentials to the phone.

## Shutdown and recovery

A login attempt has its own durable ID, owner and deadline. Explicit cancel
propagates to GrantBridge and records cancellation. Reopening the host
reconciles existing attempt state instead of starting another OAuth request.
Shutting down the local adapter does not log out a completed CLIProxyAPI
credential or authorize a hidden turn retry.

Historical native homes and `accounts add` commands belong to the earlier
architecture; they are not supported v2 account creation or execution paths.
