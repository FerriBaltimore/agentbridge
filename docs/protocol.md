# AgentBridge protocol

The current compatibility protocol is described here. The expanded target
contract, including models, instances, messages, turns, capabilities and the
unified error envelope, is in interface/README.md.

The public transport is JSON-RPC 2.0 over stdin/stdout. Each request has `jsonrpc`, `id`, `method` and an object `params`. Notifications have no `id` and receive no response. Errors contain a stable `data.code`.

## Records

`Account` identifies an engine and a home or credential reference. An account ID is immutable. Registering the same ID with a different identity is refused, and the same native home cannot be registered twice for one engine.

`Session` binds one conversation to an account, workspace, model, native session ID and optional parent. `Run` is an accepted attempt with a distinct `message_id`. Request keys make instance and run admission idempotent across restarts. A session or account can have only one active run in the SQLite store.

Events have a monotonically increasing store sequence, a run and session ID, a kind, a timestamp and JSON data. Common kinds are `session`, `assistant`, `text_delta`, `tool_call`, `tool_result`, `subagent`, `usage`, `quota`, `permission_required`, `permission_denied`, `gap`, `recovery` and `run_finished`.

A `tool_result` with `outcome: unknown` means that the provider was asked to perform work but AgentBridge did not observe its result. It is never converted to success. A `gap` means the provider emitted data that the adapter does not interpret. A `subagent` event describes only a child identity/status observed in the provider stream. It does not prove that the child completed hidden work.

## Core methods

| Method | Purpose |
| --- | --- |
| `capabilities` | Return engine capability declarations. |
| `accounts.list` | Inspect safe account references, never secrets. `accounts.register` is retained only for a managed local SDK setup and is rejected by the public RPC boundary. |
| `accounts.login.start/status/check/complete/cancel` | Persist an authentication attempt, observe it, verify it in a fresh provider process, bind the safe account projection, or cancel it. |
| `accounts.status` | Read configured identity and the latest authentication observation. `refresh: true` performs a provider account read when supported. |
| `accounts.usage` | Read the latest quota and usage observation. `refresh: true` performs a provider usage read when supported. |
| `accounts.usage_history` | Read the bounded, append-only history of account usage observations. |
| `sessions.create`, `sessions.list`, `sessions.get` | Create and inspect sessions. |
| `sessions.transfer` | Create a destination session using native or portable continuity. |
| `sessions.export` | Build a bounded portable evidence bundle. |
| `runs.submit`, `runs.get`, `runs.list` | Admit and inspect work. |
| `runs.events` | Read normalized events from a sequence offset. |
| `runs.stop` | Request cancellation of the exact run. |
| `runs.resume` | Explicitly submit follow-up work after a terminal run. |
| `runs.usage`, `runs.subagents` | Read observed usage and subagent coverage. |
| `accounts.quota` | Read the legacy local quota view. Prefer `accounts.usage` for account-service observations. |
| `recover` | Mark a lost worker interrupted without retrying it. |

The CLI validates provider-specific options before accepting a run. Unsupported controls are rejected. Account selection, retry policy, permissions and external side effects belong to the embedding application. Account status and usage are observations, not proof of future availability. Codex provides live account reads; Cursor provides cached binding checks; Claude reads bound-profile identity and native OAuth quota windows. Cursor SDK account quota remains unavailable. See interface/implementation-status.md for the implemented subset and remaining work.
