# Private GrantBridge adapter protocol

AgentBridge consumes the local GrantBridge JSON-RPC 2.0 stdio adapter. This is
a private trusted boundary, separate from public AgentBridge `accounts.login.*`
methods. The v2 proxy methods are `auth.proxy_start`, `auth.proxy_status` and
`auth.proxy_cancel`. Version compatibility must be checked against the pinned
GrantBridge checkout; older native `auth.start` and `auth.activate` are not
account onboarding methods in v2.

## Framing and ownership

Each line is one JSON object. A response has `jsonrpc: "2.0"`, the matching
request ID and exactly one of `result` or `error`. Unknown private error codes
are sanitized to `grantbridge_failed`; malformed matching envelopes produce
`provider_protocol_error`. Raw provider and Management API bodies must never
enter AgentBridge state, events or public errors.

AgentBridge scopes each durable attempt to an opaque owner. GrantBridge's
`auth.proxy_*` methods forward the sidecar OAuth state and do not persist that
attempt. A lost start response is an unresolved OAuth-start outcome unless
the sidecar state can be recovered; a supplied durable request key prevents an
automatic second start. The local host chooses the adapter executable and data
directory; public RPC cannot change them. `health` reports version and
service. Closing a sidecar does not silently restart or rerun an attempt.

## Proxy OAuth methods

`auth.proxy_start` receives the provider (`codex`, `claude` or `grok`), the
loopback proxy base URL and the management key resolved from the configured
environment variable. The key value crosses only this trusted local stdio
boundary. It is never a public RPC field, account record, log field, command
argument or Codex configuration value. GrantBridge calls CLIProxyAPI's
Management API to start the provider OAuth flow and returns sanitized
attempt state and authorization information.

`auth.proxy_status` reads the sidecar OAuth state and reports bounded
state. `auth.proxy_cancel` cancels a pending attempt when CLIProxyAPI confirms
it. An unknown or expired proxy state is `authentication_outcome_unknown`,
because it could also be a completed OAuth session. AgentBridge records that
state as interrupted and requires explicit local abandonment and proxy
restart before another attempt; a saved credential must be resolved at the
proxy first. An already granted upstream OAuth credential
is not assumed revoked by a local cancellation. The AgentBridge
login state machine maps the private status into safe public
`accounts.login.status`; a callback by itself does not mark the account
verified.

AgentBridge performs a fresh sidecar inventory, identity and model check for
`accounts.login.check` and repeats required checks in
`accounts.login.complete`. It requires exactly one active credential in an
otherwise clean sidecar and a stable identity. It commits a usable account
atomically; failed, cancelled, ambiguous or changed identity cannot bind.
The management key is required for these checks even if a later route is
pinned. CLIProxyAPI retains and renews the credential; GrantBridge does not
hand an upstream token or native home to AgentBridge.

The initial browser mode is `same_host`. A URL-only fixture or local
Management API response is not live provider acceptance. Remote callbacks,
credential refresh and authenticated model execution need separate evidence.
