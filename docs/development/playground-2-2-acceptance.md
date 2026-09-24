# Playground acceptance, AgentBridge 2.2.0

Local checks on Linux x86_64, 2026-09-24. Browser tests used simulated proxy,
Codex and GrantBridge processes; no real account was discovered or used.

## Browser and SDK

- Playwright exercises all four views at desktop and mobile widths, account
  controls, login flow, chat, activity, and route changes. In one existing
  conversation it verifies a pinned OpenAI route changing to automatic Claude,
  with the model, reasoning effort and numeric context changed before the next
  turn. It also covers automatic provider changes and a failed update retaining
  the unsent draft. All 18 browser cases and five HTTP server cases passed.
- The installed 2.2.0 x86_64 wheel served the playground with disposable state
  outside this repository. The browser loaded Overview, Accounts, Chat and
  Activity, found the three provider choices, and reported no page errors or
  horizontal overflow at 390 pixels. Screenshots are under `dist/screenshots/`.
- The disposable wheel installation and this repository's `.venv` both reported
  2.2.0 and passed `examples/installed_quickstart.py`. The installed CLI listed
  models from an explicit empty state directory.
- `python -m pytest -q` passed with 794 tests; two optional offline native
  acceptance tests were skipped because no executable path was supplied.
  Those two tests passed separately with Codex 0.153.0 extracted from the
  installed wheel. `python tools/check_repository.py` and `node --check` for
  every playground JavaScript file also passed.

## Built artifacts

| Platform wheel | Bytes | SHA-256 |
| --- | ---: | --- |
| `ferran_agentbridge-2.2.0-py3-none-manylinux_2_28_x86_64.whl` | 179,719,070 | `98d6c1ec1f6ec7e7e77923afe8c3c013c7e140df5cdbc8b7364bec41d64f4b77` |
| `ferran_agentbridge-2.2.0-py3-none-manylinux_2_28_aarch64.whl` | 167,713,287 | `8883bb75227dd4d96ad50b800ed0d7e13090bc5fc558f5218fdf20c757934eb7` |

Both wheels passed `tools/verify_bundle_wheel.py`. The ARM64 wheel was
inspected on x86_64; no native ARM64 browser or provider run was performed.
These checks establish local fixture behavior and package integrity, not live
OAuth completion, model entitlement, inference, or quota accuracy.
