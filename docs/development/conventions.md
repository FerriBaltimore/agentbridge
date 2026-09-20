# Development conventions

These rules apply to AgentBridge source, tests, documentation, fixtures and
configuration. They are enforced by tools/check_repository.py.

## Size

Every repository text file has a maximum of 450 physical lines. Blank lines and
comments count. The limit is checked against both the working tree and the
exact staged Git blobs.

Split a file when it approaches the limit. Choose the split by responsibility:
provider protocol, persistence, validation, command parsing, rendering and
orchestration are separate concerns. Do not remove documentation, compress
statements or hide source in strings to pass the check.

## Names

Python modules, functions, methods, arguments and variables use snake_case.
Classes use PascalCase. Constants use UPPER_SNAKE_CASE. Python tests start
with test_.

Markdown filenames use lowercase kebab-case, except standard contract names
such as README.md, AGENTS.md and CLAUDE.md. Other text files use lowercase
names with dots, underscores or hyphens.

Public RPC methods use plural_resource.verb, for example instances.create and
turns.events. Public fields and error codes use snake_case. CLI options use
--kebab-case. Provider-native names stay inside an adapter and are not renamed
in stored raw evidence.

Avoid modules named utils, helpers, common or misc. If a function has a clear
owner, place it in that owner's module.

## Validation

Run these checks before reporting a change:

    python tools/check_repository.py
    python -m pytest
    python -m pip wheel . --no-deps -w dist

Use python tools/install_hooks.py once per checkout to enable the staged-file
check as the local pre-commit hook. CI always runs the guard independently.
