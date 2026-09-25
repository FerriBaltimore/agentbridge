# Earned Codex rate-limit resets

This v2 operation applies to a Codex OAuth account bound through GrantBridge
to its dedicated local CLIProxyAPI sidecar. It is separate from the automatic
`resets_at` time on a quota window and from additional usage pools such as
`gpt-reserve`. A window reset time does not imply an earned credit. A reported
credit count does not prove that any particular window is eligible to reset.

The public SDK, JSON-RPC and CLI expose the same two operations:

    Bridge.account_reset_credits(account_ref, refresh=False)
    Bridge.account_quota_reset(account_ref, *, idempotency_key,
                               observation_ref, credit_id=None)
    accounts.reset_credits(account_ref?, account_id?, refresh?)
    accounts.quota.reset(account_ref?, account_id?, idempotency_key,
                         observation_ref, credit_id?)

Each call selects exactly one `account_ref` or `account_id`. An ambiguous
human name is rejected. Neither operation starts a model turn. Claude and Grok
accounts return `unsupported` for earned Codex resets.

## Read credits

`accounts.reset_credits` without `refresh` reads the last sanitized local
observation. With `refresh: true`, AgentBridge verifies the original proxy
binding and asks Codex for the current earned-credit count and, where
available, individual credit details. CLIProxyAPI substitutes the chosen
account's access token inside its local Management API; AgentBridge does not
read or store that token. A failed read retains the previous observation and
marks it stale with a safe `reason`. Missing evidence is `unknown`, not zero.
Reads begun before a reset cannot overwrite its outcome: a durable account
generation fences credit and quota observations after the provider call. While
an attempt is pending, the credit count and quota percentages are unknown;
the prior credit observation cannot authorize a new redemption.
An overlapping older read cannot replace a credit or direct quota observation
saved by another request.
After a confirmed reset, passive proxy quota headers remain unknown even if
the sidecar reports a newer timestamp. A direct upstream quota read must
confirm the new percentages.

The response includes `account_ref`, `provider: "codex"`, `status` (`available`,
`none` or `unknown`), nullable `available_count`, nullable `credits`,
`observed_at`, `age_seconds`, `stale`, `observation_ref`, optional
`pending_reset` and `reason`. `credits: null` means individual details were
unavailable; an empty array means Codex returned no detail rows. The count
comes from Codex and can exceed the number of returned detail rows. Credit
IDs are opaque; optional fields include status, reset type, grant and expiry
times, title and description. Raw provider responses are discarded.

## Redeem one credit

The caller first requests `accounts.reset_credits` with `refresh: true`, then
checks `stale: false`, `status: "available"` and a positive `available_count`.
It saves the returned `observation_ref` and a new idempotency key for **one
explicit user-authorized redemption**. The observation remains eligible for
60 seconds. Supply `credit_id` only when selecting an available ID from those
details; omit it to let Codex select an available credit.

AgentBridge saves the exact account, observation, key and optional credit ID
before sending the mutation. One observation authorizes at most one attempt.
A second request with the same key and parameters addresses that same
attempt; a different key or changed parameters cannot silently spend another
credit. An active model turn blocks a new attempt. Pausing account routing is
independent of this explicit account operation. A pending attempt blocks new
turns and local account deletion until its outcome is reconciled. After a
confirmed reset, quota percentages observed before the redemption are marked
unknown until fresh provider evidence is available.

Known outcomes are `reset`, `already_redeemed`, `nothing_to_reset` and
`no_credit`. `already_redeemed` means the original logical request completed;
it is not a second redemption. `windows_reset` is nullable unless Codex
actually reported it. After a known outcome, AgentBridge requests fresh
earned-credit information and, for a reset, account usage. A refresh failure
does not change the known redemption outcome or invent new capacity.
If a definitive preflight check fails before the provider request, the attempt
is closed as `not_started`; obtain a fresh observation before trying again.

If the mutation transport fails, the provider response is invalid or the
account binding changes after submission, AgentBridge returns
`reset_outcome_unknown`. The saved attempt remains pending. It does **not**
retry automatically or use a new account. Inspect `accounts.reset_credits`;
if the user chooses to reconcile the attempt, repeat the exact same account,
key, observation reference and credit ID. The backend idempotency key is reused.
Do not generate a fresh key for an uncertain attempt.
AgentBridge permits one dispatch of that key at a time. A concurrent request
receives `reset_in_progress`; after a process crash, the dispatch claim expires
after 90 seconds so the saved key can be explicitly retried.
If the account binding changes while an attempt is pending, AgentBridge keeps
the attempt unresolved and refuses to send the request through the new identity.

## CLI and RPC examples

```sh
agentbridge accounts reset-credits "Personal Codex" --refresh --json
agentbridge accounts quota-reset "Personal Codex" \
  --idempotency-key 8ae96ff3-3425-4f4c-8772-b6fd61502868 \
  --observation-ref SAVED_OBSERVATION_REF \
  --credit-id SAVED_CREDIT_ID --json
```

```json
{"jsonrpc":"2.0","id":1,"method":"accounts.reset_credits","params":{"account_ref":"Personal Codex","refresh":true}}
{"jsonrpc":"2.0","id":2,"method":"accounts.quota.reset","params":{"account_ref":"Personal Codex","idempotency_key":"8ae96ff3-3425-4f4c-8772-b6fd61502868","observation_ref":"SAVED_OBSERVATION_REF","credit_id":"SAVED_CREDIT_ID"}}
```

The [official Codex app-server contract](https://learn.chatgpt.com/docs/app-server)
documents earned-credit reading, idempotent consumption and the four outcomes.
The current v2 adapter uses the corresponding backend requests through the
pinned CLIProxyAPI management transport. That backend HTTP surface is private
and may change independently of the published app-server method. The request,
normalization, binding and replay rules have deterministic fixture tests.
One live Codex account returned an earned-credit count and details during a
read-only check; live redemption has not been accepted.
