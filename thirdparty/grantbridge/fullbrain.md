# Future Fullbrain host

Fullbrain can host the same GrantBridge library after the AgentBridge adapter is
proven locally. During development, AgentBridge remains a valid host and does
not need a second parent application.

Fullbrain should map its authenticated user and account scope to a GrantBridge
owner, publish only the attempt ID and safe authorization URL to the cockpit,
and keep workspace, mission and business-operation policy outside GrantBridge.

The flow should be:

1. Fullbrain admits an authenticated connection request.
2. GrantBridge starts the provider attempt and stores state and credentials on
   the execution server.
3. Fullbrain's edge publishes the authorization URL or hosted browser to the
   user's phone.
4. GrantBridge completes and checks the authorization.
5. Fullbrain records connection metadata and the operation receipt.
6. AgentBridge receives an account reference and runs the selected agent.

Fullbrain must not import AgentBridge or GrantBridge private SQLite files,
browser profiles or vault files. It should use public adapter methods and its
own guarded writers. The same account identity and credential-handoff rules
apply whether the host is the AgentBridge CLI or Fullbrain. A phone only
controls the server-side session; it does not become the account that later
runs Claude, Codex or Cursor.
