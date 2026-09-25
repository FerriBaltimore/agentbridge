# Native Codex continuation and access acceptance

Date: 2026-09-25. Runtime: the repository's integrity-checked Codex 0.153.0
archive, Linux x86_64. These are offline native-runtime experiments against
deterministic loopback Responses and management servers. No real account or
external model is used. They are not live upstream provider acceptance.

## Continuation

Objective: preserve one native Codex identity and its indexed conversation
through a model change, proxy account/provider change, and SDK process restart.

`tests/test_bundled_native_continuity.py` executes three real native turns for
each transport. A first model on account alpha is followed by another model on
alpha, then a model on beta with another provider label. The third turn runs
from a fresh Python SDK process; every native turn also has a different PID.
All account records still use the Codex engine.

Observed result: two transport cases passed. In both cases:

- All three turns completed with the same native UUID.
- The second and third Responses requests contain all earlier synthetic user
  messages and assistant answers, in order.
- There is exactly one native rollout and one native `threads` index row;
  the row's UUID and rollout path match the instance binding.
- Native `thread/list` finds that UUID and native `thread/read` returns its
  three turns after the native processes and SDK process have restarted.
- Removing only the synthetic rollout makes continuation fail. The SDK retains
  the bound UUID, sends no extra Responses request, and creates no new rollout.

Native listing must include the relevant source kinds. Codex's default
`thread/list` filter omits `exec` sessions; this runtime records the app-server
session source as `vscode`. The test explicitly includes `exec`, `appServer`,
`vscode`, and `cli`, and requests all model providers. A default listing that
omits a thread is not evidence that its native history was lost.

Reproduction:

```sh
python -m pytest tests/test_bundled_native_continuity.py -q
```

Both transport cases also passed with the final filesystem projection described
below. The rollout metadata confirms native version `0.153.0`. Assertions use
public synthetic messages and structural native metadata. No provider error
body, credential value, or reasoning content is copied into this report.

## Effective tool permissions LAB

Objective: execute a bounded Python canary through a real Codex shell tool in
all six combinations of transport and filesystem policy. Inspect actual reads,
writes, and a loopback HTTP request instead of inferring access from argv.

`tests/test_bundled_native_access.py` supplies a deterministic Responses tool
call using the native advertised `exec_command` tool. Its script attempts to
read and write a synthetic file inside the workspace and another outside it,
then requests a second loopback fixture server. It also checks whether a separate
same-user synthetic worker process and its environment are visible through
procfs. Every operation is bounded; the script reports booleans and touches no
user files.

Expected policy matrix:

| Policy | Read inside | Write inside | Read outside | Write outside | Shell network |
| --- | --- | --- | --- | --- | --- |
| read-only | yes | no | no | no | no |
| workspace-write | yes | yes | no | no | no |
| danger-full-access | yes | yes | yes | yes | yes |

Observed baseline: the two full-access cases pass the complete matrix. Four
restricted cases could not run the canary: nested bubblewrap lacked permission
to read the kernel's overflow UID metadata. Unchanged canary files alone do not
prove successful restricted execution.

Rejected candidate: enabling Codex's deprecated `use_legacy_landlock` feature
did not solve the problem. The read-only cases could not execute the shell;
workspace-write rejected a permission profile requiring direct enforcement.
The native feature listing and the actual canary failures are the acceptance
evidence. Upstream also documents the legacy backend's narrower profile
support in its [Landlock implementation](https://raw.githubusercontent.com/openai/codex/rust-v0.153.0/codex-rs/linux-sandbox/src/landlock.rs).

The next candidate created a private PID namespace before applying Landlock,
so native processes could access their own procfs without seeing host workers.
Continuation still passed, but nested bubblewrap still failed: Landlock also
prevents the mount changes needed by the native tool sandbox. A private procfs
alone is insufficient. The native workspace-write profile must retain its
protected metadata paths; weakening those protections is not an accepted fix.

Accepted candidate: normal restricted execution starts in a separate PID
namespace with a filesystem containing only approved system paths, the reviewed
runtime, the workspace, and private native state and temporary directories. The
synthetic root is read-only. This outer projection allows Codex's own nested
bubblewrap sandbox to enforce its tool policy. Inputs-only execution retains its
separate Landlock policy; full-access execution retains its explicit wider
permissions.

For workspace-write, native `sandbox_workspace_write.exclude_slash_tmp=true`
disables the implicit global `/tmp` writable root; private `TMPDIR` remains
available. Without that override, Codex tried to create protected `/tmp/.git`
on the read-only synthetic root and the tool could not start. Before making
the synthetic root read-only, a shadow outside file could be created inside
the namespace; the corresponding host file remained unchanged. The accepted
candidate prevents both that shadow write and writes to the host canary.

Observed final result: all six permission cases pass the matrix. Restricted
shells cannot see the synthetic host process or read its environment; explicit
full access can do both. The last combined run passed the unchanged read-only,
full-access, and two continuation cases in 33.32 seconds. After adding the
workspace-write `/tmp` override, both remaining workspace-write cases passed
in 6.55 seconds. Thus all eight cases have passing evidence with their final
configuration, across both transports.

The regression test requires the canary to run and its reported effects to
match the matrix. Native-runtime upgrades must rerun this test; a reviewed
version binding by itself does not demonstrate sandbox compatibility.

Reproduction:

```sh
python -m pytest tests/test_bundled_native_access.py tests/test_bundled_native_continuity.py -q
```

## Delivery validation

The final complete suite passed on 2026-09-25: **1,088 passed in 383.73 seconds**,
with no failures or skipped tests. It includes all eight native-runtime cases
above, deterministic routing and queue tests, and the Playwright browser suite.
`AGENTBRIDGE_CODEX_ACCEPTANCE_BIN` pointed to the integrity-checked bundled
runtime, enabling the optional native startup checks as well. Desktop and mobile
access-control screenshots were also inspected.

The repository checks and `git diff --check` passed. The final wheel was built,
installed into a disposable environment, and reinstalled into this repository's
`.venv`. The installed quickstart, CLI help, and capability output passed. Every
SDK Python source file matched both the wheel and the installed repository copy.

Artifact: `ferran_agentbridge-2.3.3-py3-none-manylinux_2_28_x86_64.whl`.
SHA-256:

```text
99067a34e1b3844e6613f1d51a68961155be6bceb410e7f4fe297f542a542b0f
```

This validates the SDK and its playground using synthetic accounts and local
fixture servers. Live upstream provider acceptance remains separate. No package
was published and Fullbrain's installation was not changed.
