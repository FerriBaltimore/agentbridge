# Playground acceptance, AgentBridge 2.3.0

Local checks on Linux x86_64, 2026-09-24. The browser and account tests in this
record use disposable state and simulated local proxy, GrantBridge and Codex
responses. They do not discover or use real accounts.

## Implemented browser flow

- The playground serves four views through the public `Bridge` SDK: Overview,
  Accounts, Chat and Activity. Its local HTTP server translates requests into
  SDK calls. The refresh control sits in the sidebar brand on every view.
- Overview shows account totals, usable routes, fresh usage observations and
  per-account capacity. It pages account rows to the available height. Missing
  or stale quota remains labelled unknown or stale; it is never displayed as
  zero usage.
- Accounts shows provider, identity, status and observed usage in a table. A
  row opens a dialog with that account's observed models and available model
  metadata. Add account and Remove are explicit actions; removing a route does
  not revoke the provider credential. The table becomes compact stacked rows
  on narrow screens and pages according to its available height.
- Chat chooses provider and model from SDK observations, and exposes reasoning
  effort and numeric maximum context only when the selected route reports
  them. An optional account pin and permission mode appear under More settings.
  A conversation can change provider, model and supported controls between
  turns. It restores the last viewed conversation and shows a live timeline of
  tool activity, permissions, partial responses and Codex compaction events.
  Activity displays recorded turn events; Stop remains an explicit cancellation.

The browser uses SDK account, model, usage, instance and turn operations. It
does not maintain a separate provider catalogue or directly call CLIProxyAPI.
Displayed models are local proxy observations, not proof that a provider will
accept a live turn.

## Login, recovery and account identity

The sole Add account flow asks for a provider and display name, then starts
`accounts.login.start` through GrantBridge and the account's isolated
CLIProxyAPI sidecar. For Codex and Claude, AgentBridge checks the local OAuth
callback port before dispatch: 1455 for Codex and 54545 for Claude. A known
bind conflict returns `oauth_callback_port_busy` with an actionable message.
This check cannot rule out a later race after dispatch; a lost or uncertain
start response remains `authentication_outcome_unknown`.

The SDK exposes sanitized interrupted attempts through `accounts.login.list`.
The Add account dialog lists those attempts and offers an explicit Abandon
action using `accounts.login.cancel` and the attempt's ownership references.
An uncertain start that reaches the browser also presents its safe attempt
references and the same action immediately. Listing never cancels or retries
OAuth. Abandoning closes the local attempt; it does not prove that remote
authorization was cancelled. The browser does not display provider error
bodies or credential values.

The previously running local playground used `.agentbridge` as its state root
inside the project workspace. Eight saved turns ended `interrupted` with
`unknown_outcome` after native launch failed, without a native event. The
workspace overlaps private state and is rejected by the native sandbox. The
current worker revalidates even historical sessions before native launch and
records `invalid_workspace` with a not-started outcome. Deterministic tests
cover that guard. The running playground must be restarted with state outside
the workspace for the account to execute from this project.

Account names are unique after trimming and case folding **within each
provider**. The same human name can be used for Codex and Claude, for example.
Start and completion enforce the provider-scoped name claim in database
transactions. When active accounts share a human name, the SDK emits stable
`id:<account_id>` references so the UI and callers select the intended account;
an ambiguous unqualified name is rejected.

## Validation observed

| Check | Result |
| --- | --- |
| `python -m pytest -q tests/test_playground_browser_dashboard_viewport.py tests/test_playground_login_recovery.py tests/test_account_provider_names.py tests/test_playground_browser_accounts_table.py tests/test_playground_browser_chat_viewport.py` | 26 passed. Playwright covered four views at 1440×900, 390×844 and 320×700; zero document overflow, compact Accounts rows and dialog, Chat composer, OAuth recovery and provider-scoped names. |
| `python tools/check_repository.py` | Passed the repository naming and 450-line checks. |
| `AGENTBRIDGE_TARGET_ARCH=x86_64 python -m pip wheel . --no-deps -w dist/wheels` | Built `ferran_agentbridge-2.3.0-py3-none-manylinux_2_28_x86_64.whl`. |
| Installed x86_64 wheel in a disposable Python environment | `agentbridge --version` returned 2.3.0; `examples/installed_quickstart.py` passed; `agentbridge --root <temporary-state> models list --json` returned an empty catalogue. The wheel was also reinstalled into this repository's `.venv`. |
| `python -m pytest -q` | 833 passed, 2 optional offline native acceptance cases skipped because `AGENTBRIDGE_CODEX_ACCEPTANCE_BIN` was not set. |
| Installed-wheel browser smoke | All four views loaded from the repository playground with a disposable state and the installed 2.3.0 SDK at 1440×900 and 320×700, without page errors or document overflow. |
| `node --check` for all playground JavaScript files and `git diff --check` | Passed. |

Focused Playwright also verified last-conversation restoration and live inline
events during a simulated turn. Codex model controls are projected from the
sidecar's top-level `models` client catalogue. These checks used fixtures;
they do not establish live entitlement or inference.

## Built artifacts

| Platform wheel | Bytes | SHA-256 |
| --- | ---: | --- |
| `ferran_agentbridge-2.3.0-py3-none-manylinux_2_28_x86_64.whl` | 179,720,922 | `993a4f1874ff6b5c7577c76befb7c3e91c93efee747403b30bdd9892b352dc0c` |
| `ferran_agentbridge-2.3.0-py3-none-manylinux_2_28_aarch64.whl` | 167,715,135 | `efcd8c451d64f672ae08d2b77bb4e4724543ac3a21e9071281a37e33dcff79ef` |

Both wheels passed `tools/verify_bundle_wheel.py`, which checked their platform
tags, pinned CLIProxyAPI 7.3.16, Codex 0.153.0, Node.js v24.21.0 and the
GrantBridge adapter. The ARM64 wheel was inspected on x86_64; no native ARM64
execution or browser run was performed.

## Local operating acceptance

The prior playground and its three sidecars were stopped with no active turns.
The entire `.agentbridge` state was moved atomically to
`~/.local/state/agentbridge`, outside the project workspace. Its existing
account had the same verified provider identity after relocation, but the
proxy's binding fingerprint had changed. The new fingerprint was stable across
a sidecar restart. A guarded local migration updated only that fingerprint
after confirming the existing login origin, managed route, provider, active
credential, identity and model count; a failed post-check would have reverted
it. The account then verified and all 13 saved models had fresh local proxy
observations. This administrative migration is specific to that state move;
normal routing still rejects a changed binding.

The installed 2.3.0 playground is running at `http://127.0.0.1:8765/` with
that external state. Playwright observed one ready account, a restored
conversation, visible Codex reasoning and context controls, and no page
errors. With the real state, all four views and the model dialog also fitted
1440×900, 390×844 and 320×700 without document overflow. A single live,
read-only browser turn on `gpt-6-luna` with `low`
selected completed and returned the requested short response. This is one
live inference observation, not acceptance for every model or proof that the
provider applied the selected effort internally.

Live OAuth completion, credential refresh, quota accuracy, context override
acceptance, and native ARM64 execution remain unverified. Each provider and
model needs separate live acceptance evidence. The Codex and Claude callback
ports are currently occupied by another local application, so a new browser
login cannot complete until those ports are free.

The browser playground remains in this repository and is not packaged in the
AgentBridge runtime wheel. The [provider acceptance gate](../interface/provider-acceptance.md)
tracks live-provider claims separately.
