# Capability model

Adapters declare each operation as native, adapter, fallback, unsupported or
unknown, with limitations and requirements. `declaration_scope` is
`adapter_implementation`; `runtime_provider_support_verified` is false. These
are implementation declarations, not fresh native account observations. The
declaration is part of capabilities.get, not scattered across callers.

These labels describe adapter internals and evidence, not public API names.
Callers always use AgentBridge operations such as models.list and
accounts.status. A native Codex method, a Claude CLI translation and a Cursor
SDK call must produce the same normalized response shape and stable error
codes.

| Capability | Codex | Claude | Cursor |
|---|---|---|---|
| Execute a turn | native | native | adapter |
| Account status | native app-server | adapter with provider probe limits | adapter with provider probe limits |
| Account usage and quota | native plus local observation | native OAuth windows plus turn observations | session usage; account quota unavailable |
| Model catalog | native model/list | native initialize | SDK catalog |
| Per-model effort | native | adapter | reported model parameter only |
| Context window selection | unsupported | unsupported | unsupported |
| Native continuation | native | native | SDK resume |
| Stop | process group | process group | SDK or process |
| Native transfer | version-gated | version-gated | unsupported |
| Portable transfer | adapter | adapter | adapter |
| Tool permissions | native options and host responses | native options and host responses | SDK tool policy only |
| Subagents | model-dependent | model-dependent | SDK-dependent |
| Image input | inline image input | inline image input | inline image input |

See implementation-status.md for the remaining work behind these declarations. Each operation and parameter
also carries a `maturity` field: `implemented`, `fixture_tested`,
`provider_tested` or `unsupported`. Capability defaults remain conservative even after scoped operator acceptance;
provider-tested declarations require a recorded acceptance run against that provider
and version. `capabilities.get` includes `acceptance.provider_tested: false`
unless the deployment has matching acceptance evidence.

## Parameter validation

Common parameters are limited to values with comparable meaning across engines:
model, effort, workspace_path, timeout_ms, permission_mode, sandbox_mode,
allowed_tools, max_turns and max_budget. Context-window selection remains unsupported. Bounded inline attachments and
Codex/Claude host approvals are specified in [interactive-inputs.md](interactive-inputs.md).

The reserved provider_options surface is currently unsupported. Unknown fields and
unsupported values fail admission with unsupported or unsupported_parameter; they are never
silently ignored.

## Account selection

`account_ref` is required for instance creation. Fullbrain selects an account
from the safe `accounts.list` projection, records that selection, and passes it
explicitly. AgentBridge never changes accounts silently during an operation.
