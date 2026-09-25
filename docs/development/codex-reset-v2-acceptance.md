# Codex earned resets in AgentBridge v2

Observed on Linux x86_64, 2026-09-25 (Europe/Madrid). This record separates
fixture behavior, local package checks and read-only live provider evidence.
No real earned-reset credit was consumed.

## Design carried from Fullbrain v1

Fullbrain v1 recorded one pending intent and idempotency key before calling
Codex's `account/rateLimitResetCredit/consume`, bound the request to the
expected account and kept uncertain results pending. AgentBridge v2 keeps
those rules, but uses the single GrantBridge-bound CLIProxyAPI credential for
the account rather than a separate native Codex home. The public SDK owns the
read and redemption operations; CLI, JSON-RPC and playground call that SDK.

OpenAI's [app-server contract](https://learn.chatgpt.com/docs/app-server)
defines the credit count, optional details, required idempotency key and four
redemption outcomes. The corresponding backend HTTP path used by the pinned
Codex client is private. The exact bundled CLIProxyAPI 7.3.16 binary was
probed with synthetic OAuth data and a local fixture, as detailed in
[the transport acceptance record](codex-proxy-resets-acceptance.md).

## Historical reset snapshot

The results below belong to the reset implementation recorded in commit
`946fe40`. That snapshot used schema v11 for reset observations and attempts.
Later account-affinity, native-session and reset migrations changed the
integrated source. These counts and wheel checks are not acceptance of that
later source.

| Check | Observed result |
| --- | --- |
| Repository guard and staged-content pre-commit | Passed. |
| Snapshot `python -m pytest -q` | 986 passed, 3 skipped. |
| Focused browser/server suite for resets, Accounts and usage | 32 passed. |
| Reset store/SDK/transport tests | Passed with synthetic credentials; no real account discovery. |
| Snapshot SQLite migrations | v9 conversation deletion, v10 queue tables, v11 reset observations/attempts; fixture upgrade from v9 passed. |
| Snapshot x86_64 and ARM64 wheels | Built and verified against pinned CLIProxyAPI, Codex, Node and GrantBridge resources. |
| Snapshot disposable x86_64 wheel install | Installed example and CLI version `2.3.4` passed outside the checkout. |

The skipped tests require an optional native sandbox acceptance binary or a
local GrantBridge checkout. They are not evidence of live reset redemption.
The ARM64 wheel was inspected on x86_64 and was not executed on ARM64.

## Delivered v2 reset branch

The verified source is commit `1650b95` with schema v13: v9 conversation
deletion, v10 queues, v11 account affinity, v12 native session binding and
v13 reset observations, generations and attempts. The repository guard and
`git diff --check` passed. The complete `python -m pytest -q` run passed with
1116 tests and 3 optional skips in 381.87 seconds. It includes the reset SDK,
transport, store, CLI, JSON-RPC and Playwright browser checks. The skipped
tests require an optional native acceptance binary or a local GrantBridge
checkout; no test discovered or used a real account.

| Platform wheel | Size | SHA-256 |
| --- | ---: | --- |
| `ferran_agentbridge-2.3.4-py3-none-manylinux_2_28_x86_64.whl` | 179,773,334 bytes | `3c4017c431a41b4ed2b6f5c69f4b53328508976784a66156b3df5d212484a7d6` |
| `ferran_agentbridge-2.3.4-py3-none-manylinux_2_28_aarch64.whl` | 167,767,551 bytes | `2d59d7119f054b897921fab9a815b6fc41331331b4a192dcbed46864f2c52c17` |

Both wheels passed `tools/verify_bundle_wheel.py` with the pinned CLIProxyAPI,
Codex, Node and GrantBridge resources. The x86_64 wheel was installed in a
disposable environment and in the delivery worktree's `.venv`. From outside
the checkout, the installed example, CLI version 2.3.4, reset command help,
SDK methods and capability declarations passed without accounts. The ARM64
wheel was inspected on x86_64; native ARM64 execution remains untested.

Concurrent, uncommitted execution-access work in the shared checkout is not
part of this artifact. Its separate schema-v13 candidate requires the tested
v14 compatibility migration before it can be combined with this reset build.
No live redemption has been accepted for either reset snapshot.

## Historical read-only provider check

At 11:19 CEST, the schema-v11 reset snapshot migrated a SQLite backup of the
live AgentBridge state in a temporary directory. The installed account's
existing managed proxy was used to request earned credits for one Codex
account. The SDK returned
`status: available`, `available_count: 2`, `stale: false`, `reason: null`.
The same read also passed from the disposable installed x86_64 wheel.
The account name, ID, token and raw provider body were not logged or stored
in this record. The temporary state copy was removed afterward. This verifies
read-only credit access for one live account; it does not verify redemption,
eligibility of a particular window, another account, later provider state or
the final integrated package.

## Boundaries

Redemption is explicit and requires a fresh observation reference. One
durable attempt blocks new turns, account removal and reauthentication until
its result is known. An uncertain result preserves the original key. A known
reset invalidates older quota percentages until a newer direct upstream quota
read confirms them. Passive proxy headers cannot restore those percentages.
The playground persists the key before sending and offers only an explicit
same-key retry. No background process consumes credits.
