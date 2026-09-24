# Account usage acceptance, AgentBridge 2.3.2

Checks on Linux x86_64, 2026-09-25 (Europe/Madrid). Deterministic fixtures
never discover or use real accounts. The installed local Codex observation
below is separate; no Claude account was available for live acceptance.

## Contract and upstream evidence

`accounts.usage(refresh=true)` verifies the original GrantBridge proxy binding,
then requests current Codex or Claude quota through that one CLIProxyAPI
credential. The SDK persists only normalized windows: provider label and scope,
observed and remaining percentages, period, reset, observation time and
per-window staleness. It retains old observed percentages with their original
time when refresh fails. Model catalogue and routing reads do not call the
provider quota endpoint. No model turn is started for a quota read.

The pinned CLIProxyAPI 7.3.16 source defines the [passive quota signal
allowlist](https://github.com/router-for-me/CLIProxyAPI/blob/v7.3.16/sdk/cliproxy/auth/quota_signals.go),
its [Claude 5-hour and 7-day header fixtures](https://github.com/router-for-me/CLIProxyAPI/blob/v7.3.16/sdk/cliproxy/auth/quota_signals_test.go),
and the [Management API call endpoint](https://github.com/router-for-me/CLIProxyAPI/blob/v7.3.16/internal/api/handlers/management/api_tools.go).
OpenAI's [Codex rate-limit test](https://github.com/openai/codex/blob/main/codex-rs/app-server/tests/suite/v2/rate_limits.rs)
shows the native `rate_limit` window structure used by the active Codex parser.
These source contracts and our fixtures do not establish live Claude quota
accuracy.

## Deterministic validation

| Check | Result |
| --- | --- |
| `python -m pytest -x -q` | 874 passed, 2 skipped. The skipped cases require `AGENTBRIDGE_CODEX_ACCEPTANCE_BIN` for offline native acceptance. |
| Focused quota, CLI and browser regressions | Passed, including account-window CLI output, overview detail, scoped windows and mixed-source fallback. |
| Playwright usage fixtures | Claude 5-hour/weekly windows, 102% reported overage, stale Codex value, desktop and narrow viewport dialog checks passed. |
| Claude scoped-window fixture | A provider-reported weekly Fable model-family limit retains its family scope and label. No fixed family-to-model mapping was added. |
| Mixed-source regression | A successful active quota refresh retains a distinct named pool from passive headers and labels the combined source. |
| Parallel build-stage lock check | Two processes entered the wheel staging lock in serialized order (`enter, exit, enter, exit`). |
| Repository guard, JavaScript syntax and diff check | Passed. |
| Wheel verification | Both x86_64 and aarch64 wheels include the pinned CLIProxyAPI, Codex, Node and GrantBridge components. |
| Disposable x86_64 wheel installation | Installed example, `agentbridge --version` (2.3.2) and empty-state model CLI passed outside the source checkout. |
| Repository `.venv` installation | Reinstalled 2.3.2; version and installed example passed. |

| Platform wheel | Bytes | SHA-256 |
| --- | ---: | --- |
| `ferran_agentbridge-2.3.2-py3-none-manylinux_2_28_x86_64.whl` | 179,727,337 | `ae9d6fbfb1e16e35b35c797b1d364006d6af69a4fac014315209e076a706a084` |
| `ferran_agentbridge-2.3.2-py3-none-manylinux_2_28_aarch64.whl` | 167,721,551 | `b199b666a7284b5cf3fa088a3ffd46666c42bd8799f3e41eca0fe81ccaa83c52` |

The ARM64 wheel was inspected on x86_64 and was not executed on ARM64. The
playground remains repository-local and is not in either runtime wheel.

## Installed local observation

The 2.3.2 playground is running at `http://127.0.0.1:8765/` from the
repository `.venv`, with state outside the workspace. One explicitly refreshed
Codex account returned `source: cliproxy_combined_usage`, with seven observed
windows. Two were fresh: the account `primary` window reported 63% used over
7 days, and a separate `gpt-reserve` window reported 0% used over 7 days.
Both had reset times. Five older passive windows retained their original stale
state. The playground showed the two fresh windows first.
This is one live account at one time; it does not prove values for other
accounts, providers or future observations.

Playwright opened Accounts and the usage dialog against that installed server.
It saw both percentages, traversed Overview, found no page errors, and found
no horizontal overflow with the dialog open at 390px and 320px widths. A
separate browser check clicked Refresh and retained visible quota percentages
with no page errors.
Screenshots are retained locally in `dist/acceptance-232/`.
