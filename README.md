# AgentBridge

AgentBridge is a Python SDK, local worker and JSON-RPC stdio API for running coding agents with durable state, explicit context continuity and observable evidence.

It currently has adapters for Codex, Claude Code and Cursor. It separates the application policy from the execution mechanism: AgentBridge records requests, starts an isolated worker, streams normalized events, stores unknown outcomes, and lets the host decide when a run may be retried or transferred.

## What it provides

- One API for Codex JSONL, Claude Code stream JSON and Cursor Python SDK runs.
- SQLite state with idempotent request admission and immutable account identities.
- Native session resume where a provider and compatible local layout support it.
- Portable context bundles with bounded size, archive hashes, explicit omissions and unknown tool outcomes.
- Stop, timeout and recovery semantics that do not silently execute a request again.
- Normalized text, tool, result, quota, usage, subagent and permission events.
- Optional quota and usage readers. Tokens, limits and monetary cost stay separate and carry their observation source.
- Python, command-line and JSON-RPC 2.0 stdio entry points. No network server is opened by the library.

The package does not copy credentials, private reasoning or Fullbrain state. Account records contain credential references such as environment variable names, never their values. Provider capabilities are reported as supported, partial, unknown or unsupported.

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
