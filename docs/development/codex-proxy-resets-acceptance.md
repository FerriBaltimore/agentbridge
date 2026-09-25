# Codex proxy earned-reset fixture acceptance

Date: 2026-09-25. This record covers deterministic local fixtures only. No
real Codex account was queried and no earned reset was consumed.

## Contract checked

The [official Codex app-server documentation](https://learn.chatgpt.com/docs/app-server)
defines `account/rateLimits/read` and
`account/rateLimitResetCredit/consume`, including a caller-owned idempotency
key, optional opaque credit ID and the four redemption outcomes. The pinned
[Codex 0.153.0 backend client](https://github.com/openai/codex/blob/rust-v0.153.0/codex-rs/backend-client/src/client/rate_limit_resets.rs)
uses ChatGPT's `/backend-api/wham` paths. Its backend request uses
`redeem_request_id` and optional `credit_id`; the response has a snake-case
`code` and optional observed `windows_reset`. AgentBridge maps those private
fields inside the adapter and returns safe public snake-case outcomes.

## Local binary probe

The exact CLIProxyAPI 7.3.16 binary bundled in the AgentBridge source archive
ran with a temporary isolated configuration and auth directory. A synthetic
Codex OAuth file used a fixture JWT and access token. The only upstream was a
local HTTP fixture. The probe removed its temporary files and stopped both
processes afterward.

Observed facts:

- `/v0/management/auth-files` returned one file-backed Codex OAuth entry with
  `auth_index` and the allowlisted `id_token.chatgpt_account_id` claim.
- A management `/api-call` POST with that `auth_index` forwarded the requested
  `ChatGPT-Account-Id` header and JSON request body to the local upstream.
  `$TOKEN$` was replaced by the selected synthetic credential inside the
  proxy; AgentBridge did not receive the credential value.
- The wrapper returned the upstream HTTP status and JSON body. The fixture
  returned a synthetic `reset` code. This validates transport shape only.

The pinned [CLIProxyAPI management handler](https://github.com/router-for-me/CLIProxyAPI/blob/v7.3.16/internal/api/handlers/management/api_tools.go)
accepts `auth_index`, method, URL, headers and `data` and returns
`status_code`, headers and body. Its auth-file projection exposes the Codex
account claim only when a Codex ID token parses successfully. The same binary
was used for the local probe.

## AgentBridge fixture checks

`tests/test_proxy_reset_credits.py` passed 15 synthetic tests for count/detail
normalization, null versus zero, exact proxy calls, known and unknown
redemption outcomes, binding changes and preflight refusal. The CLI/RPC tests
exercise only patched SDK methods and never submit a provider mutation.

Provider acceptance remains open. The WHAM HTTP path is private and could
change independently of the documented app-server RPC. CLIProxyAPI's generic
Management API call does not itself refresh a Codex OAuth token; a stale token
may make a credit query unavailable. A transport failure after submission
stays `reset_outcome_unknown`, with the original idempotency key retained for
an explicit reconciliation attempt.
