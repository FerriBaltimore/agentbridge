# Capability model

Adapters declare each operation as native, adapter, fallback, unsupported or
unknown, with a reason, requirements and observed timestamp. The declaration
is part of capabilities.get, not scattered across callers.

These labels describe adapter internals and evidence, not public API names.
Callers always use AgentBridge operations such as models.list and
accounts.status. A native Codex method, a Claude CLI translation and a Cursor
SDK call must produce the same normalized response shape and stable error
codes.

| Capability | Codex | Claude | Cursor |
|---|---|---|---|
| Execute a turn | native | native | adapter |
| Account status | native app-server | adapter with provider probe limits | adapter with provider probe limits |
| Account usage and quota | native plus local observation | account reader pending, local turn observations | account reader pending, local turn observations |
| Model catalog | native model/list plus fallback | static fallback only | static fallback only |
| Per-model effort | native | adapter | unsupported |
| Context window selection | unsupported | unsupported | unsupported |
| Native continuation | native | native | SDK resume |
| Stop | process group | process group | SDK or process |
| Native transfer | version-gated | version-gated | unsupported |
| Portable transfer | adapter | adapter | adapter |
| Tool permissions | native options | native options | SDK-dependent |
| Subagents | model-dependent | model-dependent | SDK-dependent |
| Image input | unsupported in the shared adapter | unsupported in the shared adapter | unsupported in the shared adapter |

See implementation-status.md for the remaining work behind these declarations. Each operation and parameter
also carries a `maturity` field: `implemented`, `fixture_tested`,
`provider_tested` or `unsupported`. The current release is fixture-tested only;
provider-tested entries require a recorded acceptance run against that provider
and version. `capabilities.get` includes `acceptance.provider_tested: false`
until such evidence exists.

## Parameter validation

Common parameters are limited to values with comparable meaning across engines:
model, effort, workspace_path, timeout_ms, permission_mode, sandbox_mode,
allowed_tools, max_turns and max_budget. Context-window selection and
attachments are explicit unsupported parameters in the current shared adapter,
because silently dropping them would change the requested execution.

Provider-only flags belong under provider_options.<engine>. Unknown fields and
unsupported values fail admission with unsupported or unsupported_parameter; they are never
silently ignored.

## Account selection

`account_ref` is required for instance creation. Fullbrain selects an account
from the safe `accounts.list` projection, records that selection, and passes it
explicitly. AgentBridge never changes accounts silently during an operation.
