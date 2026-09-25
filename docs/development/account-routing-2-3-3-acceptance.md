# Account routing and playground acceptance, AgentBridge 2.3.3

Checks on Linux x86_64, 2026-09-25 (Europe/Madrid). Fixture tests never
discover or use real accounts. Live observations below are separate.

## Behavior

The playground stores the selected dashboard view in the URL fragment. Browser
reloads and history navigation retain Overview, Chat, Accounts or Activity.
The Accounts usage dialog shows current quota windows first, with older
observations under an expandable section. It has a visible close button, a
fixed footer, Escape and backdrop closing, and keeps its footer visible at
narrow widths. Codex `primary` and `secondary` are provider window slots;
named pools use the provider's reported name. The UI derives window period,
percentage, reset and freshness from SDK data.

`accounts.pause` and `accounts.resume` are public SDK, CLI and JSON-RPC
operations. The playground calls them through its SDK-backed local API.
The SQLite v8 flag is durable and independent of authentication. Selection,
model route discovery, pinned creation and final turn admission reject paused
accounts. A turn admitted before the pause continues on its selected account.
Retrying an already created instance or turn with the same idempotency key
returns the original result without admitting new work.

## Deterministic acceptance

| Check | Result |
| --- | --- |
| `python tools/check_repository.py` | Passed. |
| `python -m pytest` | 921 passed, 2 skipped; native sandbox acceptance requires `AGENTBRIDGE_CODEX_ACCEPTANCE_BIN`. |
| Focused account pause, worker boundary and Playwright tests | Passed, including durable state, automatic and pinned fences, reload on all four views, seven quota windows and 320/390/800px layouts. |
| JavaScript syntax and `git diff --check` | Passed. |
| x86_64 and aarch64 bundled wheels | Built and verified; each contains the pinned CLIProxyAPI, Codex, Node and GrantBridge resources. |
| Disposable x86_64 wheel install | Installed example, CLI version and empty-state model listing passed outside the checkout. |
| Repository `.venv` | Reinstalled 2.3.3 and ran the installed example. |

| Platform wheel | Bytes | SHA-256 |
| --- | ---: | --- |
| `ferran_agentbridge-2.3.3-py3-none-manylinux_2_28_x86_64.whl` | 179,730,218 | `3bb970cc9b595120e5e373a8e1395eea9d6cdad76fcc1bd5449933b6eea98069` |
| `ferran_agentbridge-2.3.3-py3-none-manylinux_2_28_aarch64.whl` | 167,724,431 | `744456e0db33a67d44fe6488b4f56fffb4c9937ada35fc66eace9e4b7462decf` |

The ARM64 wheel was inspected on x86_64 and was not executed on ARM64.
The playground is repository-local and is not included in the runtime wheel.

## Live local acceptance

The installed playground at `http://127.0.0.1:8765/` showed one Codex account
with two current quota windows and five older observations. The base primary
window reported 63% used over seven days. The separately named `gpt-reserve`
primary window reported 0% used over seven days. The UI showed each window's
reset time and retained older percentages as stale. This is one account at
one observation time; it does not establish live quota accuracy for other
accounts or providers.

Playwright opened and closed the installed usage dialog, including its fixed
footer at 320px. It paused the live account, reloaded the page, observed the
durable paused state, resumed it and verified no page errors. The account was
left resumed. No provider turn was started for this acceptance. Live Claude
and Grok pause, quota and model behavior remains unverified. The isolated
OAuth browser flow has fixture coverage; no live provider sign-in was attempted.
