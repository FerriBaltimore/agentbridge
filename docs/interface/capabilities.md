# Capability model

`capabilities.get` declares support, maturity, limitations and requirements
for the one v2 execution path: Codex through a dedicated local CLIProxyAPI
sidecar. The upstream account provider may be `codex`, `claude` or `grok`.
A fixture-tested route or model catalogue is not live provider acceptance.
`declaration_scope=adapter_implementation` describes local behavior;
`runtime_provider_support_verified` requires separate provider evidence.

| Capability | V2 behavior | Acceptance limit |
| --- | --- | --- |
| Execute a turn | Codex Responses over a verified proxy route | Live acceptance by provider and model pending |
| Account login | GrantBridge coordinates OAuth through CLIProxyAPI Management API; a one-use callback relays remote Codex/Claude redirects | Fixture tested; live OAuth acceptance pending |
| Credential custody and refresh | CLIProxyAPI owns upstream credentials | Provider refresh behavior needs live acceptance |
| Account status and model list | Fresh local sidecar identity and model observations | Presence does not prove entitlement |
| Account usage and quota | Attributable observations where sidecar reports them | Missing values stay unknown |
| Continuation | One immutable Codex session per instance across model, account and provider changes; explicit failure on missing or divergent state | Deterministic continuity coverage is separate from pending live provider acceptance |
| Stop | Explicit supervised cancellation | Unknown outcome is not retried |
| Tool permissions | Codex host response channel | Provider and model behavior needs acceptance |
| Images and subagents | Codex request and event mapping where supported | Provider and model support varies |
| Selected context and MCP | Bounded package validation and private Unix-socket tool bridge | Fixture tested; live Codex and host-sandbox acceptance pending |

The historical direct Codex and Claude Code adapter matrix is recorded
as earlier implementation evidence in
[provider-acceptance.md](provider-acceptance.md). Those adapters no longer
admit new execution or account creation. The public API does not select an
engine; `model` and optional `account_ref` determine the proxy route.

Each operation and parameter carries a maturity value: `implemented`,
`fixture_tested`, `provider_tested` or `unsupported`. Provider-tested requires
recorded acceptance against the specific provider, model and version.
`capabilities.get` must not infer acceptance from a declared model or an OAuth
file. Unknown values and unsupported parameters fail admission instead of
being silently ignored.

## Account selection

Automatic routing requires fresh local Management API evidence: exactly one
active upstream auth file, a stable identity, a clean credential inventory and
the exact requested model. Both proxy client and management key references
must be present. A pinned route meets the same requirements. Fresh applicable
quota guides least-used selection; unknown quota is not zero. The route is
persisted before execution and remains fixed through the turn. A switch may
occur only before a later admitted turn and retains the instance's native
Codex session. Stop and recovery never trigger a hidden account switch, native
session replacement or rerun.

## Parameter validation

Common parameters include model, effort, workspace_path, timeout_ms,
permission_mode, sandbox_mode, allowed_tools, max_turns and max_budget where
supported. The v2 adapter accepts a positive numeric `context_window` on
`messages.create` and passes it to Codex as a per-turn setting. The model
maximum must be observed before a host exposes this control, and live upstream
acceptance is pending. Advanced instance defaults, `allowed_tools` and
`max_budget` are unsupported. Bounded inline attachments and Codex host
approvals are specified in
[interactive-inputs.md](interactive-inputs.md). The reserved provider_options
surface is unsupported until its parameters have a reviewed contract.
Selected `context_package` and `mcp` parameters use the bounded
[context and MCP contract](context-and-mcp.md).
