# GrantBridge integration sequence

## Delivered in the v2 working tree

1. AgentBridge uses one public account flow: `accounts.login.start/status/check/
   complete/cancel`, with `accounts login` as its blocking CLI form.
2. The GrantBridge stdio adapter exposes `auth.proxy_start`,
   `auth.proxy_status` and `auth.proxy_cancel`. The Python client calls these
   over a trusted private pipe and sanitizes returned state.
3. A new account starts on an empty, dedicated CLIProxyAPI sidecar. GrantBridge
   coordinates its Management API OAuth. CLIProxyAPI stores the upstream
   credential. AgentBridge records route references and safe evidence only.
4. Completion requires fresh single-credential inventory, identity and model
   checks, then binds the account atomically. Pinned and automatic execution
   use the same Management API verification.
5. Deterministic tests include a Python→Node→fake Management API subprocess.
   These tests use no real account and do not prove provider OAuth acceptance.

## Next acceptance work

- Complete controlled live OAuth from a fresh sidecar for Codex, Claude and
  Grok separately. Record provider, versions, account identity, callback mode,
  observed models and exact unsupported paths. A returned authorization URL
  alone is not a successful login.
- Verify CLIProxyAPI credential refresh, attribution and model execution with
  each provider. Observe actual quota separately; missing quota remains
  unknown.
- Exercise Codex tool requests, Stop, restart and account changes through the
  actual sidecar. A route stays fixed through one turn, including tool calls.
- Design and accept a remote browser/callback route before claiming mobile
  login. The initial local flow assumes browser and sidecar share a host.
- Maintain one credential per dedicated sidecar. Add a proxy-level credential
  pin or an external exclusive configuration lock if a deployment can change
  sidecar contents during an active turn.

Historical native GrantBridge provider homes and key resolution are
retained for old records only. They are not alternative v2 account creation
or execution paths.
