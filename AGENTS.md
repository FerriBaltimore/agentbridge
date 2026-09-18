# AgentBridge

Reusable Python SDK, CLI and JSON-RPC stdio API for Codex, Claude Code and Cursor.

- Keep the core independent of Fullbrain, machine paths, UI, mission state and credentials.
- All product-authored CLI output, help, labels and errors must be in English, regardless of the conversation language. Preserve user-supplied account names and other data as entered.
- Capability claims must distinguish implementation, fixture tests and live provider acceptance.
- Persist observable evidence before publishing it. Unknown results and missing usage are not success or zero.
- Never persist credential values, raw provider error bodies or private reasoning. Account configurations contain references only.
- Stop is explicit cancellation; recovery must not silently rerun work or change accounts.
- Preserve native version checks, divergent-session protection, bounded context and explicit omissions.
- Use deterministic subprocess tests before live provider experiments. Tests never discover or use real accounts.
- Validate with `python -m pytest`, build a wheel, install it into a disposable environment and run the installed example/CLI.
- Reinstall each delivered SDK change into this repository's `.venv` and verify it. Do not alter Fullbrain's installation from here.
- Do not publish a package or choose a public license without the owner's instruction.
