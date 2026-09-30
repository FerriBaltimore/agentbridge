# Optional PostgreSQL Store

AgentBridge remains standalone with SQLite. Install the `postgres` extra to select
PostgreSQL explicitly for a new Store. PostgreSQL 16 is the validated server profile.
This moves AgentBridge metadata, events, queues and admission state only. Codex native
SQLite homes, GrantBridge vaults and CLIProxy credential files remain native files.

```python
from pathlib import Path
from agentbridge import Bridge, PostgresConfiguration

bridge = Bridge(
    Path('/private/agentbridge/account'),
    backend='postgresql',
    postgres=PostgresConfiguration('ab_account', Path('/private/host/account.conninfo')),
    owner_ref='account-owner',
    durable=True,
)
```

The connection file contains libpq conninfo, including explicit `host`, `dbname` and
`user`. It must be an absolute, regular, owner-only file outside the Store directory,
with trusted parents and no symlinks. Use a private Unix socket or explicitly configure
verified TLS for TCP. AgentBridge rejects ambient libpq environment settings, service
files, custom connection options and password files. Connection values and raw database
errors are never returned through the SDK. The caller owns private-file provisioning.

A host administrator provisions one restricted login role and one `ab_` schema per Store:

```sql
CREATE ROLE ab_account LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 8;
CREATE SCHEMA ab_account AUTHORIZATION ab_account;
REVOKE ALL ON SCHEMA ab_account FROM PUBLIC;
```

Provision authentication and database CONNECT separately. Do not grant that role access
to other application schemas, database ownership, role memberships or administrative
functions. AgentBridge checks the owning role, privilege flags, memberships and other
schema/database ownership on each connection. It pins `search_path` to its own schema
and `pg_catalog`; database ACLs enforce cross-Store isolation. The operator must also
prevent unintended privileges inherited from PUBLIC in a shared cluster.

The optional psycopg dependency is imported only by the PostgreSQL path. Existing
SQLite callers do not need it. `Store.connect()` is the internal persistence boundary;
its PostgreSQL adapter supports the reviewed Store SQL surface, not arbitrary SQLite SQL.

## Selection and failure

A private, atomic `store-backend.json` stores the backend and PostgreSQL schema/connection
file reference. The selector is fsynced before any PostgreSQL write. Subsequent workers,
dispatchers and `Bridge(root)` reopen that authority. An unavailable PostgreSQL service
never creates or falls back to a SQLite database. A failed initial connection can be
retried against the same published selection.

Changing the selected backend or connection binding requires an explicit host migration.
Restoring a selector or finding the same connection-file path is not destination authority:
the recovery host must rebind a new destination role and private connection before opening.
Do not copy a source conninfo file into a recovered Store or an inner provider sandbox.
There is no ambient DSN selection, dual-write mode or automatic rollback to stale SQLite.

Connections are short-lived and bounded: connect/lock timeouts 10 seconds, statement
15 seconds, idle transaction 30 seconds, synchronous commit enabled. Per-Store advisory
transaction locking serializes reads and writes, including event sequence allocation.
Readers cannot advance beyond a later committed event while an earlier Store writer is
still uncommitted. Aborted sequences may leave numeric gaps; they do not hide committed
events. Different schemas do not share the advisory key. This conservative implementation
trades intra-Store read concurrency for the existing coherent Store contract.

PG uses explicit private `_ab_order` identity columns where SQLite used rowid. These
preserve insertion order; they are not a substitute for commit ordering. Existing public
records omit the private column. PostgreSQL deletions remove logical records and preserve
existing tombstones. MVCC pages, WAL, retained backups and external native copies follow
the host's retention policy; logical deletion does not claim immediate byte erasure.

## Checkpoints and physical recovery

Native checkpoint sealing, admission holds, content verification and native restore use
the same APIs on both backends. PostgreSQL does not manufacture `bridge.sqlite3` snapshots.
`checkpoints.snapshot_store` returns `checkpoint_sql_backup_required` on PostgreSQL, and
capabilities mark that operation unsupported for this backend.

Trusted hosts may call:

```python
identity = bridge.checkpoints.identity()
scope = {key: identity[key] for key in ('owner_ref', 'store_id', 'store_generation')}
observation = bridge.checkpoints.observe_store(format_version='1', params=scope)
```

The observation contains identity, schema version, generation-scoped event cursor and
native checkpoint coverage read in one repeatable-read, read-only transaction. PostgreSQL returns
`sql_position` with database, schema and the next WAL insertion LSN; its boundary is
`next_record_exclusive`. It does not return a backup, a protected frontier, a cluster
identity or an externally supplied receipt. It is a host SDK operation, not model RPC.
The host must join it to its actual cluster/timeline, verified base/WAL chain and compatible
native files. A PostgreSQL cluster restore is not an individual-chat rollback.

Shared-cluster integration must instrument every AgentBridge writer in the host's SQL
revision/barrier protocol before activation. PostgreSQL bootstrap creates all current
optional tables (permissions, diagnoses, learning, provider and credential state) so the
host can inventory and instrument them. Merely using the same server does not make two
independent application transactions atomic or include AgentBridge in another application's
protection claim. Physical recovery, migration and provider sandbox mounts are host gates.
Set `PostgresConfiguration(..., physical_guard=<signed bigint>)` to the host's shared
physical barrier key. Writers acquire its shared transaction lock BEFORE their Store lock.
The host holds the exclusive physical barrier, calls `agentbridge.checkpoint.observe_store`
without opening/bootstraping `Bridge`, commits its candidate and observes its WAL cut before
unlocking. This observation is read-only and takes neither writer lock; another Store can
continue unrelated work outside that short cut. Never observe again after uploading and
attach that newer observation to an older physical frontier.

## Explicit host migration and restore

```python
from agentbridge.storage import migrate_postgres

receipt = migrate_postgres(
    root, operation_id=operation_id, owner_ref=owner,
    postgres=configuration, verify_quiescence=verify_closed_execution_domain,
)
```

The host callback receives the same operation/owner/Store/generation and source-root inode
binding twice: before fencing/copy and before the PostgreSQL COMMIT. It must verify a closed
execution domain and current exclusive host authority. A caller boolean, a new lock unknown
to old clients or inability to read arbitrary `/proc` entries is not quiescence evidence.
Standalone callers without that verifier receive `store_migration_unsupported`.

The SDK checks legacy recorded workers/queue locks, drains SQLite, persists a migration
journal, installs permanent SQLite DML fences and publishes a pending PG selector before
copying. The transaction preserves logical rows, identity, events, queue/tombstone data,
explicit `_ab_order` and deleted AUTOINCREMENT high-water marks. A receipt in the same PG
COMMIT makes retry safe after COMMIT but before local acknowledgement. A failure stays
pending; it never writes stale SQLite or claims native credential files were copied.
Source SQLite and native files remain available for controlled recovery, never dual writes.

After restoring the exact physical SQL cut, the trusted host provisions a new restricted
role/connection and invokes `agentbridge.checkpoint.restore_postgres(destination, snapshot,
postgres=configuration, owner_ref=owner, operation_id=operation_id, workspace_paths=...)`.
The authenticated G0 PostgreSQL descriptor identifies the exact SQL frontier. The SDK
compares restored Store identity, cursor and checkpoint coverage; it does not attest the
host's physical backup. It changes generation, preserves the source cursor/replay floor,
removes imported process/credential authority and remains held. Retry reuses its SQL receipt.
Native Codex files and provider credential snapshots are restored through their own existing
APIs; these files do not become PostgreSQL data. A restored selector is never new authority.

## Focused validation

The existing contracts can run unchanged in an owned PostgreSQL fixture by loading the
opt-in fixture plugin. It creates restricted roles and schemas per temporary Store and
removes only those resources at teardown. It never discovers a database or provider.

```sh
PYTHONPATH=src:tests AGENTBRIDGE_TEST_POSTGRES_SOCKET=/private/lab/socket \
  python -m pytest -p fixtures.test_postgres_store_fixture \
  tests/test_store_backend_contract.py tests/test_postgres_store.py \
  tests/test_store_and_continuity.py tests/test_routing_store.py \
  tests/test_message_queues.py tests/test_queue_recovery.py
```

Without the plugin, the common contracts use SQLite; PostgreSQL-only tests skip unless the
explicit fixture is configured. Use a short private SSD `TMPDIR`/pytest `--basetemp` for
Unix socket subprocess tests. No accounts, provider requests or production databases belong
in these tests. The SDK tests simulate an already recovered SQL generation. Fullbrain separately gates its
installed artifact, closed execution domain, SCRAM projection and physical PG16 restore;
neither suite substitutes for a deployed cloud/PITR acceptance gate.
