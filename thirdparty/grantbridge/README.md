# GrantBridge integration for AgentBridge v2

AgentBridge uses one account authentication flow. GrantBridge coordinates
provider OAuth through a dedicated local CLIProxyAPI sidecar's Management API;
CLIProxyAPI stores and renews the upstream credential. AgentBridge creates a
usable account only after fresh identity and model verification. Codex then
executes every turn through that sidecar.

The reviewed GrantBridge checkout is separate from this Python repository.
AgentBridge communicates with its local JSON-RPC stdio adapter rather than
handling provider OAuth itself. The integration has deterministic subprocess
and fake Management API coverage. Real OAuth for Codex, Claude and Grok remains
pending. The initial browser mode assumes browser and sidecar share a host.

## Responsibility boundary

| Concern | Owner |
| --- | --- |
| Account request, durable attempt and route binding | AgentBridge |
| Browser authorization coordination and safe response projection | GrantBridge |
| OAuth session state, credential storage, refresh and Management API | CLIProxyAPI |
| Model-based account selection and Codex execution | AgentBridge |
| Host user authorization and product policy | Embedding application |

A sidecar must be prepared empty with a separate loopback port and auth
directory for each upstream account. This is infrastructure setup, not a
second account registration method. `accounts login` and
`accounts.login.start` take `provider`, `name`, `proxy_base_url`, `key_env` and
`management_key_env`. Key arguments are names of environment variables; no
value enters account configuration. Both key references are required for a
usable route, including an explicitly pinned route.

The account is not promoted after a browser callback alone. Completion checks
one active upstream credential, stable identity, a clean inventory and local
model catalogue. Live model entitlement is still unverified. Failed,
ambiguous or cancelled attempts do not create accounts. Historical native
profiles and direct adapters remain available only for reading older state;
they cannot create accounts or execute turns in v2.

See [architecture](architecture.md), [private protocol](protocol.md),
[credential boundary](credentials.md) and [acceptance](acceptance.md).
