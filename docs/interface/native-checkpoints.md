# Native checkpoints and local Store recovery

Durability format `1` is an explicit administrative mode of the Codex execution SDK.
It is separate from provider acceptance and remote backup status. No storage credentials,
remote object store, encryption key or application-specific identity lives in this layer.

```python
bridge = Bridge(private_root, owner_ref="stable-installation-owner", durable=True)
identity = bridge.checkpoints.identity()
```

The owner comes from trusted host configuration. An omitted owner uses a stable Store-derived
reference. The selected Store persists owner, Store UUID and generation UUID once. Reopening a durable
Store preserves this configuration; requests cannot rebind the owner or disable durability.
Standalone legacy Stores declare `durability.support = disabled`. Their terminal events keep
legacy behavior; the SDK does not claim `not_required` for unsupported durability.

## Turn boundary and recovery

A durable worker becomes a Linux subreaper before native launch. After the native wrapper
exits, it closes only its owned descendants, including processes that changed sessions.
Other workers and a shared proxy live outside that ownership tree. Failure to verify death
leaves continuity pending. A later PID absence alone is insufficient evidence.

Before releasing active-turn exclusion, the Store persists a barrier and terminal intent.
The SDK inventories the private native home, makes online SQLite backups of its indexes,
publishes immutable local content and its descriptor, and associates the result with the
terminal transaction. No full AgentBridge Store copy occurs on each turn. No GCS operation
is on this execution path.

A failed seal retains the historical execution result and emits `checkpoint.pending`.
The instance cannot admit another turn, change execution configuration, edit/deliver its
queue, or be deleted until its barrier clears. Other instances continue independently.
The historical terminal stays unchanged after a retry; one later `checkpoint.ready`
observation identifies the completed checkpoint. Its sequence is after the terminal,
its `final` field is false, and a retry returns the same IDs and observations.

Terminal `data.durability` is one of:

- `sealed`, with the checkpoint descriptor;
- `pending`, with barrier ID and a bounded error code;
- `not_required`, only for definitely unstarted work or an explicit ephemeral evaluation.

The local archive contains native state, indexes and metadata. Native auth/config files and
regenerable temporary/log/shell-snapshot files are excluded. Symbolic links, special entries,
hard links, unsafe paths, changed sources, unknown layouts and incorrect hashes fail closed.
Archives remain private and are not a portable public transcript. The backup owner encrypts
their bytes before remote publication. Local retention/garbage collection is deliberately not
implemented here: do not remove content still reachable from retained snapshots.

## Administrative methods and cursors

JSON-RPC exposes `checkpoints.create`, `checkpoints.restore` and `checkpoints.snapshot_store`.
Their params contain `format_version = "1"`, a canonical UUID `operation_id`, and a nested
`params` object. Scope supplied in these params is compared to the persisted trusted binding;
it does not create authority. Local paths never come from these RPC bodies.

`create.params` contains `owner_ref`, `store_id`, `store_generation`, `instance_id`, `turn_id`.
For an automatic terminal seal, use its persisted `barrier_id` as `operation_id`. A retry
verifies the same immutable object before returning its checkpoint. A process-death proof
which was never observed is not manufactured by the retry.

`snapshot_store.params` contains exactly `owner_ref`, `store_id`, `store_generation`.
SQLite online backup captures one coherent whole Store, including concurrent instances.
This whole-Store file operation is SQLite-specific. PostgreSQL returns
`checkpoint_sql_backup_required` and advertises the operation as unsupported. Its host-only
`checkpoints.observe_store(format_version="1", params=scope)` reads identity, cursor and
coverage coherently and returns a next-record-exclusive WAL position. The host must bind
that observation to its verified physical SQL recovery chain; the observation is not a backup.
Native checkpoint `create` and `restore` remain available on both Store backends. See the
[PostgreSQL guide](../development/postgres-store.md) for selection and recovery boundaries.

The descriptor reads its cursor and coverage from that copied database. Coverage includes
only contained terminal/checkpoint associations in the required ordering. A running or
pending instance is not falsely covered. Reusing an operation ID returns that same snapshot;
a new capture needs a new operation ID.

The local snapshot descriptor contains version, snapshot ID, trusted scope, `backend=sqlite`,
Store schema, capture time, full cursor, coverage and a local content reference. It contains no
remote storage location or encryption claims. A higher layer encrypts/uploads this object and
creates the remote snapshot descriptor.

Durable `turn_events`, `instance_events` and `turn_events_stream` require `cursor`, with
`format_version`, `owner_ref`, `store_id`, `store_generation`, `seq`. Bootstrap from the
capability/identity binding with `seq=0` only for a fresh Store. After restore, reconcile
`source_cursor` against the saved consumer state and explicitly adopt `replay_start` from
the new identity. Requests below this imported frontier fail; the durable feed never
republishes old events labelled as new-generation execution. Each public event returns a complete cursor. Foreign
identity/generation yields `cursor_generation_mismatch`; a cursor ahead of the Store yields
`cursor_rollback`. Never silently reset a persisted consumer cursor. Raw `Run.events` and
`Store.events` remain legacy/local inspection APIs and do not provide durable replay binding.

## Restore into a new, held root

The trusted host materializes authenticated/decrypted content privately, then calls:

```python
from agentbridge.checkpoint.snapshot import restore_store

binding = restore_store(new_root, local_snapshot, downloaded_sqlite, owner_ref=trusted_owner)
restored = Bridge(new_root)
restored.checkpoints.register_content(checkpoint["content"], downloaded_native_archive)
restored.checkpoints.restore(
    format_version="1", operation_id=restore_operation_id,
    params={"checkpoint": checkpoint, "content": checkpoint["content"],
            "destination_generation": binding["store_generation"]},
)
```

Import preserves Store ID, execution IDs, event order, request idempotency, routing exclusions,
queue versions and deletion tombstones. It creates a fresh generation and persistently holds
execution. Worker/child/dispatcher PIDs and identities from the old host lose their authority.
Copied in-flight execution becomes an explicit unknown, interrupted result, without replay.

Native restore requires matching owner, Store, source generation, historical checkpoint and
native ID. It verifies content and exact runtime compatibility before installing a private
staging directory atomically. It relocates native SQLite rollout paths to the new home. The optional trusted
`restore_store(..., workspace_paths={instance_id: destination_workspace})` validates existing private
destination directories and rebinds Store/native index workspace paths. It preserves historical
transcript text and queue versions; no path mapping is accepted from checkpoint RPC requests.
Interrupted publication can retry using the same operation; changed installed bytes fail.

After registering a native capsule, the host calls
`checkpoints.prepare_restore_workspace(format_version='1', operation_id=restore_operation,
params=restore_params)` with the same parameters as native restore. A session's current workspace
may differ from its native index's original workspace after a legitimate move. This operation
verifies the complete capsule, its historical descriptor and principal native ID, then updates
only the held source binding to that authenticated origin. The private destination remains unchanged.
Every auxiliary thread must remain within that origin; foreign paths are refused. Repeat calls are
idempotent, never release execution and never rewrite the capsule or historical transcripts.
The existing restore method, including a reviewed historical SDK, consumes that binding and still
verifies exact runtime compatibility. The result is `workspace_prepared`, with `format_version`,
`instance_id`, `checkpoint_id` and destination `store_generation`.

`resolve_content`, `register_content`, `restore_store`, `prepare_restore_workspace`,
`adopt_drained` and `release_recovery`
are host SDK operations, absent from RPC. They never fetch credentials or start a provider.
After the required native histories are restored, the host reconciles external effects, authenticates
fresh provider bindings and establishes exclusive execution authority. Only then it calls
`release_recovery(expected_generation=...)`. Unknown copied execution remains held until a
separate supported reconciliation resolves its continuity; this method cannot waive it.

For partial recovery, `release_recovery(expected_generation=..., ready_instances=[instance_id])`
releases only the listed, locally verified histories. Other instances receive persistent
`continuity_holds`, exposed by `identity()`, and cannot execute, queue, update or delete.
A restored but unreconciled instance contributes no snapshot coverage. The initial global
hold clears without discarding unknown results; another verified instance can continue.
The host may later materialize and release a still-held instance using the same API.
Omitting `ready_instances` retains the strict requirement that every remaining hold be verified.

## Legacy migration and runtime upgrades

Enabling durability does not fabricate coverage for old terminal turns. An ordinary future
turn seals the full native history, but migration must never run a user's turn just to obtain
coverage. A drained host can instead use:

```python
bridge.checkpoints.adopt_drained(
    instance_id, operation_id=stable_operation_uuid, proof_ref=private_evidence_uuid,
    verify_quiescence=trusted_host_verifier,
)
```

This method first persists a barrier on the latest terminal turn. The callback receives the
exact owner/Store/generation/instance/turn/barrier binding. It must prove the old native write
boundary is empty, for example after draining an isolated service cgroup, and keep it drained
until the call returns. Missing process IDs, a caller-provided boolean, or ordinary process
listing alone do not establish this proof. The SDK also refuses live recorded workers or
children. Only the proof reference is saved. A failed proof leaves a pending barrier; a
verified proof reuses the normal local seal and emits a late ready observation without
rewriting the original terminal or issuing model requests. Hosts must implement and validate
their actual verifier before declaring legacy coverage complete.

After worker loss, a pending checkpoint with unverified process closure can use this same
host-only proof path with its existing barrier/operation ID. A new operation cannot replace
the barrier. Failed proof stays pending; success preserves the original terminal and emits
the late ready observation. No RPC boolean can establish process closure.

A restored generation initially retains old checkpoints as historical evidence, not current
snapshot coverage. Before publishing a new recovery point, keep host execution drained and
use `adopt_drained` with fresh operation/proof IDs to reseal unchanged native history under
the new generation. The working association changes; old terminal and checkpoint observations
remain intact. The new descriptor explicitly represents host-adopted continuity in the new
generation; its execution frontier points to the copied historical sequence. It does not
claim that the old terminal was emitted in this generation. Consumers reconcile this host
adoption and the source/replay binding separately before advancing publication. This also
permits repeated restores without reusing stale workspace mappings
or treating earlier restore operations as current activation proof.

The current native layout is `codex-0-153-0-home-v1`; Store schema is 14. Compatibility includes
hashes of the native launcher and every AgentBridge Python module. A source/backend upgrade
can therefore invalidate direct restore even when Codex's version string is unchanged.
Keep the original wheel, bundled runtime and their immutable artifact digests for every
retained recovery point. Restore that point with its original pinned runtime in isolation,
verify continuity, and then perform a separately validated migration and reseal on the new
runtime. An explicitly reviewed compatibility mapping is an alternative future migration
path; this implementation has no hash override. Never discard original artifacts merely
because the current deployment uses a newer package.

## Evidence and limits

Deterministic tests cover sealed/pending terminal association, crash after immutable
publication, retry IDs, cross-instance admission, queued dispatch, cursor rollback, SQLite
snapshot under concurrent writes, unknown copied execution, native restore, scope/runtime
mismatch and owned descendant cleanup without killing an unrelated process. Offline native
acceptance uses real pinned Codex, a loopback Responses fixture and fresh private roots.
The old Store and workspace paths are unavailable while the restored native session is read
and resumed from the rebound destination workspace.
This is not acceptance against live provider credentials.

One local sample, with the 258,471,008-byte bundled binary already extracted and 32 MiB of
native state, sealed in 0.652 seconds. A 2 MiB fixture home sealed in 0.057 seconds. These are
warm local samples, not a production p95 or a guarantee for large homes. No digest cache was
added without evidence that one was needed. Remote encryption/upload and publication,
whole-application restoration, operational quiescence attestation, remote retention and
provider reauthentication are responsibilities of the integrating host.


## Migrating a drained legacy Store

`--durability required --owner-ref OWNER` is administrative launch configuration, never an RPC
parameter. A fresh Store can enable immediately. Existing legacy instances require this host-only
sequence while the whole previous execution domain is demonstrably closed:

1. `checkpoints.begin_upgrade(operation_id=..., owner_ref=..., proof_ref=...,
   verify_quiescence=callback)` persists holds before enabling mode. The callback verifies the
   real owner/Store/generation scope against the closed process domain. A failed callback leaves
   legacy mode and persistent holds; PID absence or an expired lease is insufficient evidence.
2. `checkpoints.stage_upgrade(instance_id, operation_id=..., expected={turn_id, native_id,
   after_seq}, verify_quiescence=callback)` checks actual Store history and seals the native home.
   The saved integer cursor must identify this instance and latest terminal turn, at or beyond
   its terminal. Missing, divergent or still-unconsumed history stays held. A fresh, never-started
   instance instead requires all three expected values to be null/null/zero.
3. Commit the returned original cursor binding and checkpoint association in the host SQL
   transaction, using its normal account and conversation CAS. Do not advance over unconsumed
   history or rewrite any old terminal. The later ready observation is separate from the result.
4. `checkpoints.confirm_upgrade(instance_id, operation_id=..., reconciliation_ref=...,
   verify_reconciliation=callback)` revalidates the materialized native bytes and releases only
   this instance after verifying the durable SQL receipt. Same-proof retries are idempotent;
   different proofs are rejected. Unselected instances remain held across restart.

These methods are local SDK operations and are absent from RPC. Store schema 14 migrates to 15
by adding upgrade records; no event position or Store identity changes. Restore accepts only the
reviewed 14/15 layouts, performs that table migration explicitly and then rotates generation,
invalidating imported upgrade proofs. Native checkpoint runtime hashes remain exact; restoring
older native material still requires its original pinned runtime before a reviewed migration.
