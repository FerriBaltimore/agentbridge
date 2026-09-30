# Private credential snapshots

`Bridge.credential_snapshots` is a trusted local SDK. These methods are deliberately absent
from JSON-RPC, model tools and public CLI dispatch. They expose local content references,
never credential values. The persisted Store owner and generation determine authority;
request parameters cannot supply or change that owner.
Enable the administrative durable mode before capture; legacy mode cannot create these
snapshots or change their owner after publication.

## Online writer capture

The pinned CLIProxyAPI `7.3.16-fullbrain.1` provides credential writer protocol `1`.
The SDK requires its authenticated snapshot/revision headers, bounded manifest and file
hashes at runtime; a version string or a running process alone cannot prove support.

```python
configuration = bridge.credential_snapshots.configuration()
account_ids = configuration['cliproxyapi']['account_ids']
capture = bridge.credential_snapshots.capture_online(
    operation_id=stable_uuid, account_ids=account_ids)
bridge.credential_snapshots.verify_current(
    snapshot_id=capture['snapshot_id'], account_ids=account_ids)
```

Capture uses the real writer's cross-process lock and durable inventory without stopping
native turns or restarting the sidecar. Login/account holds remain admission boundaries;
refresh, management uploads and persisted cooldown changes participate in the writer cut.
Snapshots contain auth JSON and cooldown files, excluding ephemeral OAuth callback files.
A changed writer revision makes the historical snapshot stale. Retry the same operation
only to recover its immutable result; use a new operation to capture a newer revision.
Observation performs no heartbeat SQL writes. No local observation establishes cloud custody.

Configuration checks managed account bindings and the pinned dependency-free login adapter.
Unknown legacy/custom credential layouts remain unsupported, never silently absent.
Completed local account retirement excludes that account from recoverable credentials;
incomplete retirement stays pending. This is a semantic tombstone, not a process namespace
closure assertion. The host must preserve retirement in its SQL cut and external revocation
state, and recheck configuration before claiming current protection or activating a restore.
The SDK refuses to promote any retired account's restored credential.

`agentbridge.checkpoint.availability.inspect(root)` distinguishes an empty/missing Store
from an existing one without creating SQLite files. Unknown files, links and unsafe ownership
fail closed. Existing legacy identity still requires the explicit trusted upgrade operation.

Tests cover the final Go writer binary with a synthetic credential and concurrent native
protocol fixture A/B in a loopback-only network namespace. They establish local capture and
continued execution, not live provider refresh or inference acceptance.

## Offline capture under a host boundary

```python
capture = bridge.credential_snapshots.capture(
    operation_id=stable_uuid, account_ids=managed_account_ids,
    proof_ref=quiescence_proof_uuid, verify_quiescence=host_verifier)
private_path = bridge.credential_snapshots.resolve_content(capture['content'])
```

The descriptor implements `credential_local_snapshot` v1 with component `cliproxyapi`,
members `['proxy_auth']`, account references and `restore_authentication: 'unverified'`.
Only `managed-proxies/accounts/<account_id>/auth` is captured. The capsule contains private
plaintext credential material: keep it private and encrypt it before remote publication.
The host builds the separate remote descriptor after verified upload. This SDK supplies
no remote storage, retention, key custody or claim that a backup is protected.

Capture persists holds for exactly the selected managed accounts, preventing admission,
queued execution, login and proxy provisioning while existing work drains. Active turns
or unfinished logins yield `credential_snapshot_pending`; the same operation can retry
after the host drains them. Holds survive restart and never expire because a PID vanished.
Already queued messages remain queued. Other accounts and their sidecars remain available.

The supervisor stops only the selected account's recorded, verified process group and
retains its original route/port for later normal use. The mandatory host verifier receives
`owner_ref`, `store_id`, `store_generation`, `account_ids` and `barrier_id`. It must prove
the entire account writer boundary closed, including escaped helpers, and keep it closed
until capture returns. A boolean from RPC or absence of one PID is not such proof.
No direct filesystem/SQLite writer outside the SDK is covered without this host boundary.

Capsules reject links, special files, traversal, duplicate members, changed files and
oversized input. File hashes, private permissions and directory fsync protect publication.
The operation ID fixes immutable bytes; failed capture preserves the hold for retry.
Successful retry resolves existing bytes and never silently substitutes newer credentials.
Raw callback exceptions and paths cannot enter error messages.

Restore the SQLite Store first with `checkpoint.snapshot.restore_store`; it begins in a
new held generation. Every imported managed account acquires `restore_pending`, and imported
credential verification proofs are invalidated. Historical execution events remain intact.

```python
bridge.credential_snapshots.register_content(capture['content'], decrypted_private_file)
result = bridge.credential_snapshots.restore(capture)
```

Restore accepts only a held Store with the matching owner and managed account references.
It checks all capsule members before atomically installing each private account directory.
An existing nonmatching directory cannot be overwritten; an interrupted multi-account
restore remains held and can retry the identical descriptor. Sockets, route records,
process metadata, client keys and management keys are never restored. No proxy or login
starts from materialization, and no credential is presumed usable from its hash alone.

`verify_restored(account_id, proof_ref=..., verify_authority=..., verify_credential=...)`
is a separate trusted host operation. The authority callback must first establish exclusive
ownership and reconcile the prior machine. The supervisor then creates a new private route
while normal account admission stays held. Local management observations must match the
saved provider identity. The host credential callback receives scope, route references and
sanitized observation, and must perform provider-specific authentication verification;
returning `authentication: 'verified'` also requires the matching `identity_fingerprint`.
It must not merely echo a local file's claim of validity.

Unknown, revoked, identity-mismatched or failed verification stops that account's probe
sidecar and preserves its hold. Retired accounts cannot be promoted. A successful check
persists the regenerated route and proof before releasing that account; a retry with the
same proof is idempotent. Other proofs cannot reuse that success. Native Store recovery
release is separate and cannot bypass an outstanding credential hold.

This does not reverse provider-side rotation or revocation. Reconnection may be necessary;
restoration never silently logs in, executes inference or rewrites historical terminals.
The next Store restore invalidates these proofs again, including after a second backup.

Deterministic tests use isolated subprocess CLIProxy fixtures, a real SQLite snapshot/restore,
two account processes, queued messages, failed proof retry, unsafe paths, rejected authority,
revoked verification and a second restoration. No test discovers real provider accounts.
Production requires a real host quiescence verifier and provider verifier before this
capability can claim protected or authenticated recovery.

## Reconnect an unusable restored credential

The trusted host can authorize a new login without releasing normal execution:

```python
bridge.credential_snapshots.authorize_reconnection(
    account_id, proof_ref=authority_proof_uuid, verify_authority=host_authority_verifier)
```

This also supports a SQL-only recovery whose managed account is `restore_pending` and has
no credential capsule. It requires the saved historical provider identity. The authority
callback receives the exact owner, Store, current generation, account and operation proof.
It must also fence every prior writer of that account's auth directory, including escaped
helpers, until quarantine completes. The supervisor's process-group stop alone cannot prove
that an external writer is gone. These are trusted host obligations, not an RPC assertion.
The operation stops only that account, moves old auth files into private quarantine under
the current generation/proof, and creates a new local route with fresh runtime keys.
Quarantined credentials are never loaded by CLIProxy or included in a new auth capture.

A persisted `reconnect` hold allows ordinary owned login operations, while proxy ensure,
instance creation, inference and queued execution remain blocked. Normal account APIs do
not accept a flag that bypasses the hold. Only a new login created after authorization,
checked against the historical provider identity and atomically bound to the account,
releases it. Merely checking a login does not release admission. Rejected identity checks
persist a failed attempt and stop its sidecar; the host can explicitly authorize a fresh
reconnection proof to quarantine that failed credential and retry. Historical attempts and
unknown outcomes are not rewritten as successes.

Retry the same accepted proof after losing a response; it neither starts another login nor
rotates a prepared route again. A proof cannot authorize another account or generation.
Preparation failures retain the hold and retry safely after the same authority check.
A second Store restoration invalidates every imported reconnection authorization.
An in-progress login copied with SQLite becomes explicitly interrupted with unknown outcome;
its previous status is retained in private recovery evidence. Terminal history stays intact.
The owner must abandon that interrupted attempt through the existing login-cancel operation
before creating another login. Import never resumes its old callback or reports it successful.
