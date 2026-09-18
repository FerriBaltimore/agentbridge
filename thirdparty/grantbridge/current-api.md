# Current APIs and integration gaps

This inventory describes the reviewed source revisions in the [index](README.md), plus the local adapter now used by AgentBridge. A source implementation, a deterministic test and a real-provider acceptance result are different evidence.

## AgentBridge today

| Surface | Existing behavior |
| --- | --- |
| [`Account`](../../src/agentbridge/models.py) | Stores `id`, `engine`, `home`, name/email, `env_names`, `key_env` and an optional command. No `credential_ref` field exists yet. |
| [`Bridge.register`](../../src/agentbridge/client.py) | Registers an account reference. Registration is not authentication. |
| [`AccountService`](../../src/agentbridge/accounts.py) | Stores identity/status and usage observations. Its provider status probe supports Codex only. |
| [`Bridge.submit`](../../src/agentbridge/client.py) | Resolves named secrets from the parent's environment, redacts their values and passes them through the worker's private stdin pipe. No pluggable secret resolver exists yet. |
| [`worker`](../../src/agentbridge/worker.py) | Sets `CODEX_HOME=account.home` or `CLAUDE_CONFIG_DIR=account.home`. The inherited `HOME` is not replaced by a per-account home. |
| [`cursor_worker`](../../src/agentbridge/cursor_worker.py) | Passes the value named by `key_env` to the Python Cursor SDK. The parent must supply it. |
| [`Store.account`](../../src/agentbridge/store.py) | Rejects changes to an existing account's serialized configuration, including its home. Native-session continuity depends on preserving that binding. |
| [`CLI`](../../src/agentbridge/cli.py) | Offers `accounts add/list/status/usage/history/check/login`. Login uses the local GrantBridge adapter; relogin and logout are not exposed yet. |

AgentBridge's core remains usable with externally prepared accounts. The optional GrantBridge adapter preserves that path and is only needed for interactive login.

## GrantBridge today

Paths below refer to the separate GrantBridge repository at the reviewed revision.

| Surface | Existing behavior |
| --- | --- |
| `src/index.mjs: GrantBridge.startProvider` | Starts a provider asynchronously. It can return `starting` before an authorization URL is available. |
| `getAttempt`, `listAttempts`, `cancelProvider` | Return owner-scoped attempt state or cancel an active attempt. The local adapter exposes these operations as `auth.get` and `auth.cancel`. |
| `checkProvider` | Runs a separate verification worker. Check `verification` and `probeError`, not just the unchanged `authorized` status. `inference: true` starts an actual model request. |
| `submitProviderCode` | Supplies a code to a pending Claude process. It is a fallback, not a generic callback receiver. |
| `src/store.mjs: Store.home` | Creates `<dataDir>/profiles/<attempt-id>`. The AgentBridge sidecar activates the provider-specific `.codex` or `.claude` subdirectory after authorization; GrantBridge itself does not own the stable AgentBridge account ID. |
| `src/process.mjs: isolatedEnv` | Sets `CODEX_HOME=<attempt-home>/.codex` and `CLAUDE_CONFIG_DIR=<attempt-home>/.claude`. AgentBridge needs those subdirectories, not the outer attempt directory. |
| `src/providers/cursor.mjs` | Saves an SDK-issued key in the vault; the login requests a 24-hour TTL. There is no public credential-resolution API. Expiry must not be treated as refreshable OAuth without provider support. |
| `src/http.mjs` | Express callbacks for generic OAuth, Google and MCP only. Native Claude/Codex completion and Cursor polling do not use these routes. |
| `src/catalog.mjs` | Claude: `browser`/`hosted`; Codex: `browser`/`device`/`hosted`; Cursor: `browser`. `browser: mobile` is a location label, not a remote-access mechanism. |
| `src/browser/` | Hosted Chrome and frame/input mechanics exist. The npm package does not include the standalone web UI or its browser routes. A consumer still needs authenticated presentation and transport. |

The manifest says Node `>=20`, but the source imports `node:sqlite` and dependencies have their own runtime requirements. Do not use that manifest alone as proof of compatibility. The recorded local runs used Node 24.16.0; use that baseline for the initial package/adapter acceptance and verify additional runtimes separately.

## Gaps that must be closed

- A versioned Node JSON-RPC entry point and Python client, with bounded shutdown. The adapter lives at `scripts/agentbridge-adapter.mjs` in the GrantBridge checkout.
- Stable account binding for relogin, identity-safe promotion and a private Cursor secret resolver remain open. Initial native profile activation is implemented for Codex and Claude.
- Coordination between native CLI credential writes and relogin. Generic OAuth refresh leases do not coordinate native CLI writers.
- A sanitized status vocabulary distinguishing local credential presence, authenticated network use, quota and model execution.
- Owner-authenticated hosted-browser transport and callback routing for remote development.
- Public API tests: current protocol tests exercise the standalone application extensively, but do not prove the Python-to-Node integration.

Two existing wrapper paths also need regression checks before claiming broader OAuth integration: `startMcp` can throw after its provider returns a prepared URL normally; the HTTP helper reports `authorized` for any non-throwing callback, although denial can return a cancelled attempt. The adapter must report the actual persisted state. These are source-review findings, not fixes delivered by this documentation.

The SDK exposes internal objects today, but a consumer must not make `store.vault`, internal filenames or `coordinator` its integration contract. Add explicit GrantBridge exports before implementing the handoff described here.
