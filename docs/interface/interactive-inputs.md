# Interactive permissions and attachments

This is the implemented v1 subset, tested with Codex 0.153.0, Claude Code
2.1.266 and Cursor Python SDK 1.0.31. Applications must still validate the
installed provider version. It does not require a network server.

## Permissions

Codex and Claude turns with permission_mode=default use the native duplex
protocol (Codex app-server or Claude stream-json control messages). A provider
request is persisted before permission.required becomes visible through
turns.events or instances.events. Its data contains permission_id, operation,
input and expires_at (epoch seconds). The host applies its own approval policy.

    permissions.respond(turn_id, permission_id, decision, reason?, expires_at?)

Only allow or deny for that individual request is accepted. There is no
session-wide grant, tool argument rewrite or automatic approval. The response
is committed before delivery and returns state=queued. A later
permission.responded event with state=delivered means the decision was written
to the native pipe; it is not proof the requested tool succeeded. Tool events
and the final turn outcome remain authoritative.

An identical response is replayable after client restart. A different response
for the same permission_id raises idempotency_conflict. A response for the wrong
turn is rejected. Expired requests are denied; Stop kills the supervised process
group. A queued allow that expires before delivery can be delivered as deny;
applied_decision reports the delivered value on replay. A lost worker requires
explicit recovery, never a new automatic run or replay of a tool approval.
The request deadline is bounded by the turn timeout and 600 seconds. The host
must not rely on permission decisions surviving a native provider restart.

The dontAsk policy remains noninteractive. For Claude, other supported native
permission modes retain their own rules. Allowed tools may be approved by the
provider's configured policy without prompting the host. Cursor SDK 1.0.31 has
no host approval response channel: interactive mode fails at admission and
capabilities.get reports permissions.respond as unsupported. An observed
provider permission event alone does not imply a usable response channel.

## Attachments

messages.create accepts a nonempty text content plus up to eight attachments,
with at most five MiB combined decoded bytes:

```json
[
  {"type": "text", "name": "notes.txt", "text": "Review these notes."},
  {"type": "image", "name": "diagram.png", "media_type": "image/png", "data": "BASE64"}
]
```

Images accept PNG, JPEG, WebP and GIF. Admission validates the encoding, header,
size and fields; the provider validates the complete image and model support.
Paths, URLs, PDFs and other binary documents are rejected. Text is appended as
labeled user content. Images become native Codex data URLs, Claude image blocks,
or Cursor SDK images. No temporary public upload or implicit file read occurs.

The normalized content is part of durable admission and idempotency comparison.
Known credential values are redacted from text and names before persistence.
Accepted image bytes are stored privately with turn inputs for explicit replay;
applications must not attach credentials or private material they cannot send
to the selected provider. Message lists and events contain names, media types
and SHA-256 descriptors, without repeating image data. Portable transfer includes
those descriptors and explicitly marks attachment_content_omitted. Native
continuation uses the provider's existing history. Portable transfer does not
silently retransmit attachments to another account or provider.

## Catalogues and quota

models.list(..., refresh=true, account_ref=...) reads Codex model/list, Claude's
initialize response or Cursor.models.list without submitting a model turn.
Catalogue presence is not an entitlement check. Missing metadata stays unknown;
refresh failures return a marked static fallback with the failure reason.

Claude account status reads only the bound native profile. loaded_only does not
prove provider access. Explicit account usage refresh reads the native OAuth
usage endpoint and projects only reported utilization windows and reset times.
This is native OAuth compatibility, not a promised stable public API. The
response identifies provider_contract=native_oauth_compatibility. On failure,
a previous observation remains available but stale; missing usage never becomes
zero. OAuth secrets remain in the bound profile and request memory.

Cursor exposes model enumeration and per-agent usage through its SDK. Its
session token/cost observations are not remaining account quota. Account usage
returns supported=false, stale=true, reason=sdk_account_quota_unavailable.
The separate Team Admin API requires an administrator key; it is outside this
user-account SDK contract. No dashboard scraping or ambient browser login is
used to manufacture a quota result. Historical time filters and aggregation
remain unsupported. Codex duplex token updates are session cumulative and are
labeled accordingly, rather than being reported as the current turn's total.

Sources: [Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Claude SDK](https://code.claude.com/docs/en/agent-sdk/python),
[Cursor Python SDK](https://prod.cursor.com/docs/sdk/python),
[Cursor Admin API](https://prod.cursor.com/docs/account/teams/admin-api).
