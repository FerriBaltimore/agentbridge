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
native checkpoint coverage read under one Store barrier. PostgreSQL also returns
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
Lock order also matters: writers own the Store advisory lock before table triggers run.
Do not hold an exclusive host SQL barrier and then wait for `observe_store` if those triggers
can wait for that barrier. Observe first and revalidate the common change marker at the cut,
or establish one reviewed transaction-guard order before activating combined protection.

A SQLite migration must drain all writers, preserve every logical row, primary key, cursor,
queue and tombstone, and copy source rowid to `_ab_order` for `accounts`, `sessions`, `runs`,
`deleted_instances`, `provider_inspections` and `checkpoint_restore_operations`. Reseed
identity sequences after importing IDs. Verify parity before atomically changing selection;
after a PG write, returning to the old SQLite database requires a reverse migration.

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
in these tests. Native restore tests simulate an already recovered SQL generation; they
are not evidence of an implemented physical PostgreSQL backup or host migration.
