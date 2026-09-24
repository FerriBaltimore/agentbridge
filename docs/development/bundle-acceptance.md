# Bundled runtime acceptance, 2026-09-24

This record distinguishes built artifacts, local subprocess checks and live
provider acceptance. No real account or provider request was used.

## Built artifacts

| Platform wheel | Size | SHA-256 |
| --- | ---: | --- |
| `ferran_agentbridge-2.1.0-py3-none-manylinux_2_28_x86_64.whl` | 179,717,767 bytes | `87d966c46a5e8cfd04af17813cab1ce8877cba83a911e69a7a7a0370797b0814` |
| `ferran_agentbridge-2.1.0-py3-none-manylinux_2_28_aarch64.whl` | 167,711,982 bytes | `205cc95a924a4e8de14a78e2cf1a3da95d56ab5f78d3bd09523b32ac34b972db` |

Both wheels passed `tools/verify_bundle_wheel.py`: each platform tag matches
its archive hashes in `bundle/lock.json`, and each wheel includes the pinned
GrantBridge source set and required upstream licenses and notices. The build
validated the ELF architecture of CLIProxyAPI, Codex and Node in both wheels.
The ARM64 wheel was inspected on x86_64; no native ARM64 execution has been
performed yet.

## Linux x86_64 local checks

- `python tools/check_repository.py` passed.
- `python -m pytest` passed with 784 tests; 2 optional offline Codex acceptance
  tests were skipped because their executable path was not supplied. Running
  those 2 tests with the extracted bundled Codex path passed.
- A disposable Python environment installed the x86_64 wheel. The installed
  CLI reported version 2.1.0; `examples/installed_quickstart.py` and
  `agentbridge models list --json` passed without accounts.
- The installed bundle launched Codex 0.153.0 and Node 24.21.0, served the
  GrantBridge `health` request, completed start/status/cancel against a fake
  local Management API, and started and retired an actual local CLIProxyAPI
  7.3.16 sidecar. This check used `PATH=/usr/bin`, without an external Codex
  or Node executable on that path.
- The extracted Codex binary returned version 0.153.0 under AgentBridge's
  Landlock launcher. The native contract check identified the reviewed
  Codex 0.153.0 binding.
- The same wheel was reinstalled into this repository's `.venv`; the
  installed CLI and example passed there.

These checks establish local packaging and process behavior. They do not
establish live OAuth, provider entitlement, token refresh, inference, quota
accuracy or ARM64 native acceptance. Existing account sidecars keep their
running executable until an explicit host drain and restart.
