# CLIProxyAPI local integration lab

Date: 2026-09-23

## Objective and hypothesis

Determine whether AgentBridge can run Codex through a locally controlled
CLIProxyAPI process while preserving an attributable account boundary. The
initial hypothesis was that a single local Responses endpoint would work with
Codex's tool loop, and that account selection would need an explicit route.

This is deterministic fixture evidence. It does not establish acceptance by a
live provider or subscription account.

## Setup

- AgentBridge SDK from this repository's `.venv`.
- `codex-cli 0.153.0`.
- CLIProxyAPI built from commit
  `673131f57484517c3a1eae7e36c4cfa7b9bb4efc` with Go 1.26.0.
- Proxy bound to `127.0.0.1`, with temporary configuration, auth directory and
  home. The Codex home and upstream HTTP server were also temporary.
- All client and upstream credentials were ephemeral fixtures. No real account,
  OAuth file or Fullbrain installation was read or changed.
- CLIProxyAPI's `-local-model` flag disabled remote model catalogue fetching in
  the routing experiments. Management access was disabled.

## Observations

| Experiment | Observable result |
| --- | --- |
| Codex CLI to a direct mock Responses endpoint | A streamed final answer completed with exit code 0. A separate function-call fixture executed `exec_command`, sent the tool result in a second `/v1/responses` request and completed. |
| Codex CLI through CLIProxyAPI to a mock Chat Completions upstream | A streamed final answer completed. A separate function-call fixture translated through the proxy, executed `pwd` in Codex, returned the tool result upstream and completed. |
| AgentBridge SDK to Codex CLI to CLIProxyAPI to a mock upstream | `Bridge.register` → `session` → `submit` → `wait` returned `completed` and text `LAB_OK`. The proxy received one `/v1/responses` request; the upstream received one streamed `/v1/chat/completions` request. |
| Two prefixed upstream routes with `force-model-prefix: true` | The model catalogue listed `a/lab-model` and `b/lab-model`, with no bare `lab-model`. The bare request returned HTTP 400. Each prefixed request returned HTTP 200 and reached its corresponding fixture credential. |
| Codex CLI with a slash in the model ID | `codex exec -m cuenta/modelo` sent the literal model ID to a mock Responses server and completed. Codex 0.153.0 warned that model metadata was missing and used fallback metadata. |
| Two credentials in one proxy route, `request-retry: 0` | The first upstream credential returned HTTP 429. In the same client request the proxy tried the second credential and returned HTTP 200. |

The experiments prove request routing and a local tool loop with fixture
providers. They do not prove OAuth login, live provider compatibility, actual
quota visibility, session continuity across providers, or correctness of an
account change during a turn. The fake upstream's token usage is synthetic.
The tool fixture covered one `exec_command` function call; Codex can also send
other tool shapes and hosted tools that this lab did not exercise.

## Integration decision

Use CLIProxyAPI as the required local execution route. For AgentBridge v2, run
**one proxy instance per upstream account**, each with a separate port and auth
directory.
AgentBridge sets a distinct Codex home per conversation, records the selected
account before each turn and uses bounded portable context when a new account
is selected. Keep the account fixed for the turn, including requests following
a tool call. Do not treat the proxy's internal failover as an AgentBridge
account transfer: the 429 experiment shows that `request-retry: 0` does not
prevent a credential change.

Earlier direct Claude adapter acceptance is historical; new turns must run
through Codex and a proxy account. Account quota reported by Codex under a
proxy may describe the gateway or be unavailable; record upstream quota as
unknown until it has its own
observable source. CLIProxyAPI documents round-robin, weighted-round-robin and
fill-first routing; AgentBridge performs its own quota-aware account selection
between turns using attributable observations.

Account onboarding uses one flow: `accounts login` with provider and account
name. AgentBridge now prepares an empty dedicated local sidecar by default,
with an isolated auth directory and transient client and management keys held
by its supervisor. A compatible CLIProxyAPI executable must be installed or
selected with `AGENTBRIDGE_CLIPROXY_BIN`. Advanced callers may provide a
proxy URL and environment variable names for the two keys to use an existing
isolated sidecar in the same OAuth flow. GrantBridge coordinates the
sidecar's Management API OAuth; CLIProxyAPI stores and renews upstream
credentials. AgentBridge creates the account only after fresh identity and
model checks and never stores key values in its database. The management key
is required even for a pinned route. This login has
deterministic fake-Management-API subprocess coverage, not live OAuth
acceptance. The initial browser mode assumes the browser and sidecar share a
host.

## V2 fixture result

The v2 integration test `tests/test_v2_routing_integration.py` uses two local
Management API fixtures and a deterministic Codex executable. It passed model
discovery, automatic least-used selection, a quota-driven account change,
portable context on the change, native continuation on the same account,
pre-execution fallback after an account admission race and refusal to resume a
pinned route after its upstream identity changed. These are fixture results;
the executable is not a real provider.

An actual CLIProxyAPI instance with one configured `openai-compatibility` key
returned zero `auth-files` entries even though its configuration contained one
upstream key. A config-backed `codex-api-key` also returned zero. That API lists
file-backed/runtime credentials rather than every possible upstream route.
AgentBridge v2 therefore requires an observable one-credential binding and a
clean config inventory before automatic selection. Config-key-only routes are
currently ineligible. An external sidecar reconfigured during a turn remains
outside AgentBridge's lock; strict pinning requires a fixed sidecar
configuration or a proxy-level credential pin.

The observer was also run against the built CLIProxyAPI binary: one synthetic
file-backed Codex OAuth credential passed the identity and inventory checks;
the same sidecar with an additional configured Codex API key was rejected.
The synthetic OAuth file did not authenticate to a live provider, so this is
local proxy-shape acceptance rather than provider acceptance.

The next acceptance gate is a controlled live turn on one isolated OAuth
account, followed by multi-turn continuation and account changes. Test quota
exhaustion and provider-specific tools before enabling production selection.

## Sources

- [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
  documents custom provider `base_url`, `env_key` and Responses transport.
- [CLIProxyAPI's Codex guide](https://help.router-for.me/agent-client/codex)
  documents a local `/v1` Responses endpoint.
- [CLIProxyAPI configuration](https://github.com/router-for-me/CLIProxyAPI/blob/main/config.example.yaml)
  documents prefixes, `force-model-prefix`, routing and retry settings.
- [CLIProxyAPI source](https://github.com/router-for-me/CLIProxyAPI)
  is the tested upstream project; the commit above pins this lab's build.
