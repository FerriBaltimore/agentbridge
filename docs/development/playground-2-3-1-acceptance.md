# Playground and event streaming acceptance, AgentBridge 2.3.1

Checks on Linux x86_64, 2026-09-25. Deterministic tests use disposable state,
simulated proxy responses and provider subprocesses. They never discover or
use real accounts. The single live observation below is separate.

## Implemented contract

- `models.list` retains observed default and maximum context windows per
  account and offers only values that fit every observed eligible account for
  automatic routing. Unknown ceilings yield no override choices.
- Chat selects one of those SDK values or leaves the provider default. It no
  longer accepts an arbitrary token count. Admission checks a fresh selected
  account ceiling and filters automatic candidates before balancing. An
  unsupported override returns `context_window_unavailable` without creating
  a turn. Nominal context windows are distinct from usable prompt tokens and
  from output token limits.
- `Bridge.turn_events_stream` replays normalized public events by `seq`, then
  follows them in bounded pages. Its idle timeout does not imply completion.
  `turns.events` through JSON-RPC supports a bounded long poll with the same
  cursor. Both use the SDK's provider-neutral event projection.
- The local playground sends all normalized turn events over SSE, with event
  IDs for reconnection. Chat renders incremental activity and message updates;
  regular SDK reads reconcile state if the connection drops. Closing the SSE
  connection does not stop or retry a turn. Stop is explicit.

Codex's [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
defines `model_context_window`. Its [model schema](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/openai_models.rs)
identifies `max_context_window` as the override ceiling and reserves headroom
from the nominal window. The selector mirrors the default and maximum values
observed from the local proxy catalogue, as Fullbrain v1 did; it does not
invent provider options.

## Deterministic validation

| Check | Result |
| --- | --- |
| `python -m pytest -q` | 859 passed, 2 skipped. The skipped cases require `AGENTBRIDGE_CODEX_ACCEPTANCE_BIN` for offline native acceptance. |
| Playwright fixture coverage | Context choices, cross-account safety, SSE delivery of tool, compaction and message delta events before terminal, terminal close, and mobile layout passed in the full suite. |
| SSE transport fixture coverage | Sequence replay with `Last-Event-ID`, invalid cursor rejection and safe transport errors passed. |
| `python tools/check_repository.py`, `node --check` for playground JS, `git diff --check` | Passed. |
| `tools/verify_bundle_wheel.py` for both 2.3.1 wheels | Passed; contains pinned CLIProxyAPI 7.3.16, Codex 0.153.0, Node.js v24.21.0 and the reviewed GrantBridge adapter. |
| Disposable x86_64 wheel installation | Installed example, `agentbridge --version` (2.3.1), and an empty-state `models list --json` passed from isolated `site-packages`. |
| Repository `.venv` | Reinstalled the 2.3.1 x86_64 wheel; version and installed example passed. |

| Platform wheel | Bytes | SHA-256 |
| --- | ---: | --- |
| `ferran_agentbridge-2.3.1-py3-none-manylinux_2_28_x86_64.whl` | 179,722,716 | `8dee856a58832a823313b9b8e431c8356bac784d52ecc2e903230f36263fa18a` |
| `ferran_agentbridge-2.3.1-py3-none-manylinux_2_28_aarch64.whl` | 167,716,930 | `630c094c1938624d889f33817fb86380c77692e48538d4380f0517d6fe19ae71` |

The ARM64 wheel was inspected on x86_64; it was not executed on ARM64.
The playground stays in this repository and is not part of the runtime wheel.

## Local live observation

The installed 2.3.1 playground is running at `http://127.0.0.1:8765/` as the
user service `agentbridge-playground-231.service`, with state outside the
workspace. A refreshed SDK catalogue returned 13 observed models; all had
context choices. For `gpt-6-luna`, the SDK returned a 272,000 token default
and an 872,000 token maximum. Playwright displayed those choices with no page
errors.

One browser turn requested the 872,000 token context option and low reasoning
effort, received a short `OK` response and finished `completed`. The saved
turn recorded the requested context and effort with no error code. The browser
received ten normalized events through one SSE connection, including message
completion, usage and turn completion. The provider did not report an
effective context window for this turn, so this observation does not prove
the exact runtime window applied. It also does not establish acceptance for
other models, accounts or providers.

The separate [provider acceptance gate](../interface/provider-acceptance.md)
still tracks live OAuth, quota, model and ARM64 claims.
