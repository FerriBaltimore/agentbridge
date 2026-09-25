# Interactive permissions and attachments

The v2 execution path uses Codex through CLIProxyAPI. Local fixture tests
cover the input and event shape; each upstream provider and model still needs
live acceptance. Earlier direct-provider results are historical evidence in
[provider-acceptance.md](provider-acceptance.md).

## Permissions

Permission and sandbox settings can be saved per instance and overridden per
turn. See [execution access](execution-access.md) for inheritance, full access
and the selected-context boundary.

Codex turns with permission_mode=default use its supervised duplex protocol.
A provider
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

The dontAsk policy remains noninteractive. Allowed tools may be approved by
Codex's configured policy without prompting the host. An observed upstream
permission event alone does not imply a usable host response channel.

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
labeled user content. Images become Codex data URLs. No temporary public upload
or implicit file read occurs.

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

`models.list(..., refresh=true)` observes exact IDs from configured proxy
accounts without submitting a model turn. A local model catalogue is routing
evidence, not entitlement. Metadata absent from the sidecar is unknown.

Account quota is reported only when the proxy provides a fresh, attributable
observation. A stale observation remains stale; missing usage never becomes
zero. Codex duplex token updates are session cumulative and are labeled
accordingly, rather than being reported as the current turn's total.
`accounts.usage(refresh=true)` can obtain current Codex or Claude quota through
the bound proxy OAuth credential, without a model turn. Windows remain
separate, including Claude weekly and scoped pools; the SDK reports the
provider's percentage, period, reset and per-window freshness where known.
Unverified scopes do not influence model routing. Fixture coverage establishes
the request and normalization shape, not live provider acceptance.
Historical time filters and aggregation remain unsupported.

Source: [Codex app-server](https://learn.chatgpt.com/docs/app-server).
