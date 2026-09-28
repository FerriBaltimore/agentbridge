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

These cases, and the host-isolated case, need a host which lets the unconfined
bundled bubblewrap create its own user namespace. With
`kernel.apparmor_restrict_unprivileged_userns=1`, AppArmor confines that
launcher as `unprivileged_userns` and denies its uid map
(`bwrap: setting up uid map: Permission denied`); every restricted native turn
then ends `interrupted` with `unknown_outcome`, which is the intended fail-closed
behavior rather than a defect. The shared test mark probes that permission and
skips these files with an explicit reason. Run them from a process already
confined by a profile granting `userns`, as the recorded runs inherited from the
IDE profile, or from a host-configured launcher profile. Do not weaken the host
policy or remove the isolation to make them pass.

## Historical 2.3.3 delivery validation

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

## 2.4 host-isolated full-access LAB

Objective: retain real native shell, workspace writes, selected context and MCP
inside a trusted host worker while denying service state and other tenants.
The standard `danger-full-access` profile retains its existing meaning; the host
must explicitly select `host_isolated: true`. Inputs-only modes reject it.

A normal nested system bubblewrap failed under the host's stacked unprivileged
AppArmor profile. Inside an explicitly permitted test environment, a second
barrier remained: the outer procfs contains locked read-only child mounts, so
Linux refuses another procfs mount. Mounting only the inner procfs read-only or
with `subset=pid` was insufficient. An empty procfs was rejected because native
executables need genuine `/proc/self/exe`; fabricated executable links were not
accepted. No host security settings or system executables were changed.

Accepted namespace experiment: a separately compiled, reproducible bubblewrap
0.11.1 adds `--proc-ro` without changing `--proc`. The trusted host worker has
its normal procfs and a second pristine read-only procfs. Native execution gets
a fresh PID namespace and fresh read-only procfs; it never receives the helper
mount. The exact static executable produced identical bytes in three builds.
Its source, patch, dependency versions, license notices and build recipe are
recorded alongside the separately locked `native_bwrap` resource. The upstream
Codex package remains unchanged.

The real host experiment inherited the existing IDE profile's explicit user
namespace permission. Native PID 2 resolved `/proc/self/exe` to the actual
Python interpreter and could not see `/run/native-proc`. The production-style
probe then loaded the SDK's exact inherited seccomp filter after namespace setup:
ordinary child processes and threads worked, while further user namespaces were
denied. This replaces a nested sysctl write with a process-local restriction;
no host policy is weakened. Fullbrain probes the exact locked launcher and SDK
filter before advertising the capability, and fails before turn admission when
the host service profile cannot support it.

The combined integration test is
`tests/contract/agentbridge/native_context/test_host_isolated.py` in Fullbrain.
It uses actual bundled Codex inside the production worker, with only synthetic
local Responses/account fixtures. Its three-turn sequence includes queued
context/MCP delivery, model and account changes, and dispatcher loss followed by
exact-message context rebind. Service-file and sibling-socket canaries remain
inaccessible while shell, child-process and thread workspace writes succeed.
A single native UUID must survive the complete sequence. Live upstream provider
acceptance remains separate.

The completed native sequence also exposed known app-server notices being
misclassified as gaps. The pinned executable's generated JSON Schema confirms
`configWarning`, `warning`, `deprecationNotice`, remote-control connection
status, MCP startup status, and native goal clearing. These now produce typed
`provider.notice` metadata. A `userMessage` item acknowledges existing admitted
input without duplicating its text. Unknown notifications/items remain explicit
gaps, future lifecycle states remain errors, and selected MCP startup failure
cannot silently remove admitted tools. The combined test requires all three
final assistant answers and no unexpected gap; terminal state alone is not its
success criterion.

A separate regression appeared only when the native temporary directory was
outside `/tmp`: the projection had relied on its parent directory being created
incidentally. Explicitly creating the isolated `/tmp` directory restores the
nested native tool sandbox without adding a writable host path. All four
restricted native canaries passed after this correction, followed by all six
permission cases and both native continuity cases.

The final source fixture is based on commit
`73a9c2e412891a1daab61465b1c86cbf13b21940` plus the reviewed working tree.
Fullbrain archive SHA-256:

```text
11f8d4fd0aad6e034af125ddd7c429b18d7bca4ae13b5309808e99947fb9552b
```

Final collection contains 1,135 cases. The broad Python run completed 1,054
cases and exposed the four `/tmp` projection failures. Those four passed after
the fix; the related native/normalization/event regression run passed 70 cases
in 45.59 seconds. The final notice and continuation gate passed 46 cases in
5.25 seconds, including both new structural negative/positive tests. Browser
coverage completed 74 cases in its broad run, and its one diagnostic-metadata
expectation passed unchanged in a 3.24-second rerun after that temporary
instrumentation was removed. All 75 collected browser cases have passing
evidence. The two optional real-native offline checks were enabled.

Fullbrain's final pinned integration and binding/manifest/MCP checks passed
seven cases in 11.80 seconds with native access required. That includes all
three actual native replies, exact operation-bound MCP calls, queue context
recovery and unchanged native identity. The same run checks private file/socket
denials and actual shell, process and thread writes. No live upstream account
is part of this evidence.

The final 2.4.0 wheel and source distribution were built and verified. The wheel
was installed into a disposable environment and this repository's `.venv`.
All 144 selected package files matched the source, wheel and both installations;
installed CLI help, quickstart and the reviewed native-launcher extraction passed.
The source distribution includes the launcher patch, recipe and provenance.

Wheel: `ferran_agentbridge-2.4.0-py3-none-manylinux_2_28_x86_64.whl`.
SHA-256:

```text
a9879f709927aa105aa376917e1b5741274df25536d005a31659e80103d66229
```

Nothing was published. Fullbrain consumes the separate immutable source archive;
its Python environment does not install this SDK wheel.
