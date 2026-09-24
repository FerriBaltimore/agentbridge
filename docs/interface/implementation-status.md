# Implementation inventory

Reviewed 2026-09-23 against the AgentBridge and GrantBridge working trees.
The v2 execution and onboarding contract has one route: Codex through a
local CLIProxyAPI sidecar. Older direct adapters and their account records are
historical evidence and read-only data, not a second usable workflow.

## V2 implementation and evidence

| Surface | Implemented behavior | Evidence and limit |
| --- | --- | --- |
| Account login | `accounts.login.start/status/check/complete/cancel` and blocking `accounts login` use provider `codex`, `claude` or `grok` and name; optional advanced route references select an existing isolated proxy within the same flow | Deterministic Python→Node→fake Management API subprocess coverage; real OAuth pending |
| Managed sidecar | AgentBridge prepares one loopback CLIProxyAPI process and auth directory per account; the proxy executable requires an absolute `AGENTBRIDGE_CLIPROXY_BIN` pin outside model-writable workspaces | Linux `memfd` is required; local lifecycle checks do not establish live provider acceptance |
| Auth custody | GrantBridge coordinates the sidecar Management API browser flow; CLIProxyAPI owns upstream credential and refresh; the supervisor generates local keys and passes them to authorized local processes as needed | No credential values or raw provider errors in AgentBridge records; the client key reaches Codex but the management key does not; live refresh pending |
| Login binding | New sidecar must be empty; complete requires one active upstream identity and observed models; account is created atomically | Identity and inventory fixture checks; local observation is not live entitlement |
| Proxy account routes | Codex Responses endpoint on loopback, key references, unique sidecar URL, identity binding and clean inventory check | One file-backed account per sidecar; config-key-only routes ineligible |
| Model routing | Exact model support, fresh proxy observation, quota-aware selector with unknown fallback and persisted route decisions | Fixtures; provider/model acceptance and quota completeness pending |
| Instances and turns | Automatic account selection or pinned proxy route; fixed route for a turn; portable context on a later account change | Cross-account continuation has fixture evidence only |
| Account and model RPC | Safe account projection; exact configured IDs and separate observed accounts | Local catalogue presence is not provider entitlement |
| Usage | Source and timestamp retained; missing or stale quota remains unknown | Upstream quota varies by provider and needs live acceptance |
| Stop and recovery | Explicit cancellation; lost workers classified without silent replay | Unknown effects stay unknown; no hidden account change |
| Native isolation | Direct Codex and app-server subprocesses run under Landlock filesystem and seccomp process-inspection restrictions; private state, supervisor authority and procfs are outside their read scope | Deterministic synthetic-secret fixtures and offline Codex app-server startup passed; live provider acceptance pending |
| Account retirement | A tombstone fences new routes; managed sidecar success requires supervisor stop evidence and a durable store attestation | An unverified managed stop remains `unknown_outcome`; external proxy processes are not stopped, and read-only status exposes `retirement.managed_proxy` and `retirement.local_proxy_stopped` |
| Selected context and MCP | Bounded v1/v2 context packages, separate Codex developer/skill/evidence channels and operation-bound private Unix-socket MCP forwarding | Deterministic subprocess fixtures; live app-server, provider and host-sandbox acceptance pending |
| Historical direct records | Read-only status, events and earlier acceptance evidence | No new native login, account creation or turn execution |

The management key is required for login, local identity checks and every
proxy turn, including a pinned turn. It is never sent to Codex. CLIProxyAPI
may change credentials inside one request if a sidecar contains more than one,
so each route uses one dedicated auth directory and a fixed configuration
throughout a turn. AgentBridge checks before execution; it cannot lock an
externally managed sidecar against concurrent reconfiguration.

`contracts.inspect` and historical release records remain available for
offline schema audit. Inspecting Claude Code does not enable it
as an execution engine or account onboarding route.

## Explicit limits

- The browser OAuth route currently assumes browser and sidecar on the same
  host. Remote browser/callback behavior needs separate implementation and
  acceptance.
- Live login, refresh, model entitlement, provider-specific tools, quota
  accuracy and cross-account continuation remain unverified for the v2 path.
- PDFs and other binary attachments, persistent advanced defaults,
  provider_options and historical usage aggregation remain unsupported where
  not separately declared. Per-turn context-window override accepts a positive
  token count; the playground enables it only when the proxy model API reports
  a maximum. Provider acceptance for that override remains pending.
- A configured model ID or successful local inventory check does not certify
  an upstream model request. Missing metadata is unknown, not a default.
- A lost response to the initial Management API OAuth start can leave a
  sidecar session without a saved state ID. A supplied durable request key avoids an
  automatic second start, but the orphan needs explicit inspection or cleanup.
- A previously accepted direct Codex or Claude Code operation does not
  count as live acceptance of a proxied turn.

## Historical provider evidence

Controlled direct-provider acceptance on 2026-09-20 covered native login,
verification, binding, execution, continuation, idempotency and Stop for
Codex and Claude Code. The exact scope and release matrix remain in
[provider-acceptance.md](provider-acceptance.md). These results are retained
for audit and compatibility reads; direct execution is disabled in v2.

The [CLIProxyAPI lab](../development/cli-proxy-api-lab.md) used fake upstreams
to exercise Codex Responses requests, a tool loop and route selection. The v2
integration tests use local Management API fixtures and a deterministic Codex
executable. The login subprocess test exercises Python→GrantBridge adapter→
fake Management API. None of these is live provider acceptance.

Production enablement requires a pinned AgentBridge/GrantBridge/CLIProxyAPI
version set, installed smoke checks, live acceptance for each selected
provider and model, and restart/Stop testing through the actual host client.
Fullbrain integration is outside this repository. General production
readiness is not established.
