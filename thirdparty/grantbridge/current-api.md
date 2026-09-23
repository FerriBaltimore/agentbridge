# Current integration inventory

This is the v2 working-tree contract. Implementation, deterministic fixture
coverage and live provider acceptance are separate claims. See
[implementation status](../../docs/interface/implementation-status.md) for the
current evidence boundary.

| Surface | V2 behavior |
| --- | --- |
| AgentBridge `accounts.login.*` | One durable account flow for provider `codex`, `claude` or `grok` with name, proxy URL and key environment references |
| AgentBridge CLI `accounts login` | Blocking wrapper for the same flow; `login-start/status/check/complete/cancel` expose it asynchronously |
| `GrantBridgeClient.proxy_*` | Trusted Python client for private GrantBridge adapter methods |
| GrantBridge `auth.proxy_start/status/cancel` | Coordinates OAuth using CLIProxyAPI Management API and returns sanitized attempt state |
| CLIProxyAPI | Stores and renews upstream credentials; exposes local inventory, identity and model observations |
| AgentBridge route observer | Requires one active credential, stable identity, clean inventory and exact model before binding or execution |
| Codex worker | Sends Responses traffic through the selected sidecar; management key is not passed to Codex |

The sidecar must exist and be empty before new account login. Its client and
management keys are read from named environment variables. AgentBridge
resolves the management key for the trusted local adapter and observer but
never persists it. Public account projections omit proxy URL and key
references. A pinned account requires the same management verification as an
automatically selected one.

The Python→Node→fake Management API subprocess test exercises this integration
without real accounts. Real OAuth for each provider, credential refresh,
model entitlement, quota and account-change continuity remain unaccepted.
The initial browser flow assumes browser and sidecar are on the same host.

The older `auth.start`, `auth.check`, `auth.activate` and private native
credential handoff are historical GrantBridge APIs. Their presence does not
create a second AgentBridge onboarding or execution route. Historical direct
records are read-only.
