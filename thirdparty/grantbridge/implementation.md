# Implementation sequence

This is the planned work after the documentation pass. The files and APIs
below are proposed boundaries, not existing code.

## Stage 1: make the adapter explicit

1. Add a small public GrantBridge adapter entry point for JSON-RPC over stdio.
   It should expose version information, provider catalog, `auth.start`,
   `auth.get`, `auth.cancel`, `auth.check` and a bounded shutdown.
2. Keep one sidecar instance per configured data directory. Derive the owner
   from the authenticated host session and never trust an arbitrary owner
   supplied by a client.
3. Define a versioned safe projection. Do not expose `store.vault`, internal
   filenames, raw callback queries or provider exception bodies.
4. Make cancellation idempotent at the adapter boundary and add request-key
   idempotency for starts.

## Stage 2: activate accounts safely

1. Add a stable account binding that keeps the existing AgentBridge account ID
   separate from GrantBridge's temporary attempt ID.
2. For Claude and Codex, activate an isolated native home only after identity
   and fresh-process checks pass. Preserve the old home while a relogin is
   pending or fails.
3. For Cursor and token APIs, add a private credential resolver. Reuse
   AgentBridge's existing worker secret pipe rather than putting values in
   argv, SQLite or a command prompt.
4. Coordinate native CLI writes, account checks, relogin and active sessions so a
   credential update cannot replace a home while a worker is using it.
5. Add disconnect and reauthorization operations with explicit states and
   auditable evidence.

## Stage 3: make remote use a product surface

1. Add an authenticated host route for the hosted browser frame and input
   transport. The route must bind the browser attempt to its owner and expire
   with the attempt.
2. Keep native loopback callbacks on the server. Use hosted Chrome, device
   polling or a registered HTTPS callback when the operator is on a phone.
3. Show the same safe state on CLI and mobile: starting, awaiting user,
   exchanging, authorized, checking, failed, cancelled and expired.
4. Add reconnect and restart behavior. A sidecar restart must reconcile pending
   attempts; it must never silently start a second login.

## Stage 4: verification gates

- AgentBridge deterministic suites remain green.
- GrantBridge deterministic suites, package build and installed smoke remain
  green.
- A cross-repository fake-provider suite proves activation, relogin, cleanup
  and secret non-disclosure.
- Separate live acceptance proves Claude, Codex, Cursor, Google and a
  configurable OAuth provider from a fresh account, including authenticated
  operation and restart.
- The report names the exact provider, runtime, account, operation and
  untested paths. A URL-only smoke is never reported as a successful login.
