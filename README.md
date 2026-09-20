# AgentBridge

AgentBridge is a Python SDK, local worker and JSON-RPC stdio API for running coding agents with durable state, explicit context continuity and observable evidence.

It currently has adapters for Codex, Claude Code and Cursor. It separates the application policy from the execution mechanism: AgentBridge records requests, starts an isolated worker, streams normalized events, stores unknown outcomes, and lets the host decide when a run may be retried or transferred.

## What it provides

- One API for Codex JSONL, Claude Code stream JSON and Cursor Python SDK runs.
- SQLite state with idempotent request admission and immutable account identities.
- Native session resume where a provider and compatible local layout support it.
- Portable context bundles with bounded size, archive hashes, explicit omissions and unknown tool outcomes.
- Stop, timeout and recovery semantics that do not silently execute a request again.
- Restart-safe idempotency for instance creation, message admission, authentication and transfers.
- Normalized text, tool, result, quota, usage, subagent and permission events.
- Optional quota and usage readers. Tokens, limits and monetary cost stay separate and carry their observation source.
- Quota windows and reset countdowns, explicit Codex earned resets, model-scoped Claude limits and safe session failure classification. See [usage and failures](docs/usage-and-failures.md).
- An independent account service for configured identity, authentication observations, quota snapshots and usage history. It does not supervise worker processes or choose fallback accounts.
- Python, command-line and JSON-RPC 2.0 stdio entry points. No network server is opened by the library.

The package does not copy credentials, private reasoning or Fullbrain state. Account records contain credential references such as environment variable names, never their values. Provider capabilities include support and maturity, so fixture-tested behavior is not presented as live provider acceptance.

Native operations use [reviewed provider release bindings](docs/provider-versioning.md).
Use `agentbridge contracts list`, `contracts check ACCOUNT`, and
`contracts inspect --engine codex` to inspect compatibility. Unknown releases
require review; compatible releases share one content-addressed contract.

Authentication integration with GrantBridge is available through a local stdio adapter. GrantBridge owns the browser flow, encrypted credentials and native profile. Public responses contain safe authorization state. The trusted execution layer receives a verified native home or temporarily resolves a Cursor key over a private pipe.

## Minimal Python use

```python
from agentbridge import Account, Bridge, RunOptions

with Bridge(".agentbridge") as bridge:
    bridge.register(Account(
        id="codex-main",
        engine="codex",
        home="/path/to/codex-home",
        env_names=("CODEX_API_KEY",),
    ))
    session = bridge.session("codex-main", ".", model="gpt-5-codex")
    run = bridge.submit(session["id"], "Inspect the tests and report the first failure.",
                        options=RunOptions(sandbox="read-only"),
                        request_key="first-test-inspection")
    for event in run.events(follow=True, timeout=900):
        print(event.kind, event.data)
    print(run.wait())
```

Account status and usage do not require submitting a model request:

```python
status = bridge.account_status("codex-main", refresh=True)
usage = bridge.account_usage("codex-main", refresh=True)
```

The first Codex adapter uses the documented app-server account and rate-limit
reads. It stores observations with their source and timestamp. A missing or
stale observation remains unknown, never zero. Claude now reads bound-profile identity and native OAuth usage windows. Cursor
provides live model catalogues and session usage; its SDK has no account quota
reader or interactive approval response channel. See
[the supported input and observation contract](docs/interface/interactive-inputs.md). Managed Cursor bindings can be checked for
local expiry/revocation; that observation is marked as cached provider verification.

The same checks are available from the CLI. Account homes and credential
options are references only, so pass environment variable names, never their
values:

```bash
agentbridge accounts add --engine codex --home "$CODEX_HOME" \
  --name "Personal Codex" --email ferran@example.com
agentbridge accounts list
agentbridge accounts status "Personal Codex" --refresh
agentbridge accounts usage "Personal Codex" --refresh
agentbridge accounts check "Personal Codex"
```

The provider-neutral target interface is documented in
[docs/interface/README.md](docs/interface/README.md). It keeps accounts,
models, conversation instances, messages, turns, usage and events separate.
The current sessions/runs names remain supported while that contract is
introduced incrementally.

Repository structure is guarded by a 450-line limit for every text file and by
Python and filename conventions. Run python tools/check_repository.py during
development. Enable the staged-content pre-commit hook once per checkout with
python tools/install_hooks.py; CI runs the check independently.

To authenticate and register a new native account, keep the login process attached while
you complete the provider flow in the browser:

```bash
agentbridge accounts login --engine codex --name "Development Codex" \
  --grantbridge-root /home/ferran/grantbridge
agentbridge accounts status "Development Codex" --refresh
```

For a restart-safe integration, use the asynchronous commands. `login-start`
returns an attempt and owner reference immediately; then call `login-status`,
`login-check`, `login-complete` or `login-cancel` with those references.

The command starts `scripts/agentbridge-adapter.mjs` from the GrantBridge checkout,
prints the provider authorization URL, waits for confirmation, performs a fresh
provider check, and then registers the account. GrantBridge keeps its encrypted vault
and profile under its own data directory. AgentBridge never stores a token, code or
provider error body. Codex and Claude activation requires a verified native
profile. Claude requires an explicitly requested inference check (`--inference`)
because its local status read alone does not prove provider access. Cursor now binds
a non-secret GrantBridge reference and resolves its API key over a private pipe at
execution time. Expired or locally revoked bindings are rejected. Remote revocation
is only observed on a provider request. No default Cursor login is discovered.

The asynchronous login worker survives caller exit. `login-check` is asynchronous;
wait for checking=false and status=verified before completing. Tests use isolated
fixtures, not real accounts. See [implementation inventory](docs/interface/implementation-status.md)
for remaining features and production acceptance requirements.

All CLI help, labels and messages are in English. User-supplied data is displayed as entered.
The account name is the unique identifier used by the CLI. AgentBridge generates
the internal account ID automatically and does not ask the operator to invent one.
Running `agentbridge` without a command opens the root help, and
`agentbridge accounts` opens the account command help. Help is colored when it
is printed to an interactive terminal, remains plain for pipes and redirects,
and follows the `NO_COLOR` convention for environments that disable ANSI
formatting.

For callers in another language:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"capabilities","params":{}}' \
  | agentbridge --root .agentbridge rpc
```

A provider's permissions still apply. For live accounts, use a protected environment or credential provider and never put a token in `Account` or a prompt. The example tests use only a fake executable. Cursor support is optional: `pip install 'ferran-agentbridge[cursor]'`.

## Development

```bash
python -m pytest
python -m pip wheel . --no-deps -w dist
python -m venv .venv
.venv/bin/pip install --no-deps dist/ferran_agentbridge-0.1.0-py3-none-any.whl
.venv/bin/agentbridge --version
.venv/bin/agentbridge capabilities
```

The repository is deliberately unlicensed until Ferran chooses a license. Do not publish a package or transfer Fullbrain code into a release without an explicit review of the extraction and provider contracts.

## Provider protocol references

- Codex non-interactive JSONL exposes thread, turn, item and usage events: [official documentation](https://learn.chatgpt.com/docs/non-interactive-mode).
- Claude Code headless mode provides stream JSON and permission controls: [Claude Code documentation](https://code.claude.com/docs/en/headless).
- Cursor's Python SDK provides local/cloud agents, resume, stream messages, subagent messages and usage: [Cursor SDK documentation](https://prod.cursor.com/docs/sdk/python).
