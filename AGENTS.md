# AgentBridge

Reusable Python SDK, CLI and JSON-RPC stdio API for Codex execution through local provider proxies.

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

## Repository structure and naming

- Every repository text file must have at most 450 physical lines, including comments and blank lines. This includes tests, docs, fixtures, configuration and thirdparty notes. Split files by responsibility before crossing the limit. There is no legacy size allowlist.
- Keep one responsibility per module. Do not compress statements, remove useful documentation or hide code in strings to meet the limit. The size gate is a ceiling, not proof of cohesion.
- Read [development conventions](docs/development/conventions.md) before adding files or public API names. Use domain folders as features grow; avoid catch-all utils/helpers/common modules.
- Python files, functions, arguments and variables use snake_case; classes use PascalCase; constants use UPPER_SNAKE_CASE. Markdown uses kebab-case except standard README.md, AGENTS.md and CLAUDE.md names.
- Public API methods use plural_resource.verb, fields and stable codes use snake_case, CLI options use --kebab-case. Native provider keys stay inside adapters.
- Run `python tools/check_repository.py` and `python -m pytest`. CI rejects violations. The repository's `.githooks/pre-commit` also checks exact staged contents, so an unstaged fix cannot hide an invalid commit.
- The interface in [docs/interface/README.md](docs/interface/README.md) is the target contract. Implemented methods must have deterministic tests; provider-specific claims still require separate live acceptance evidence. Keep legacy compatibility names clearly distinguished from the target methods.
