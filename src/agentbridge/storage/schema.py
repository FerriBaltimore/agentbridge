"""The shared Store schema and reviewed migrations, applied transactionally."""

import sqlite3

from ..errors import BridgeError
from ..routing.schema import migrate_v4, migrate_v5, migrate_v11
from ..evaluation.schema import migrate_v6
from ..account_retirement import migrate_v7
from ..account_pause import migrate_v8
from ..instance_deletion import migrate_v9
from ..queueing.schema import migrate_v10
from ..native_sessions import migrate_v12
from ..execution_policy import migrate_v13
from ..checkpoint.state import migrate as migrate_v14
from ..checkpoint.upgrade import migrate as migrate_v15
from ..checkpoint.state import migrate_mode as migrate_v16


def initialize(db):
    if isinstance(db, sqlite3.Connection):
        db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS metadata(version INTEGER NOT NULL);
        INSERT INTO metadata SELECT 3 WHERE NOT EXISTS(SELECT 1 FROM metadata);
        CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY, config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS proxy_bindings(
            account_id TEXT PRIMARY KEY,
            binding_fingerprint TEXT NOT NULL,
            identity_fingerprint TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
            cwd TEXT NOT NULL, model TEXT, native_id TEXT, parent_id TEXT, context TEXT,
            created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS instance_metadata(
            session_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'active',
            version INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            account_id TEXT NOT NULL, state TEXT NOT NULL, prompt TEXT NOT NULL,
            options TEXT NOT NULL, request_key TEXT UNIQUE, created REAL NOT NULL,
            updated REAL NOT NULL, worker_pid INTEGER, worker_identity TEXT,
            child_pid INTEGER, child_identity TEXT, stop_requested INTEGER DEFAULT 0,
            error TEXT, exit_code INTEGER);
        CREATE UNIQUE INDEX IF NOT EXISTS active_session ON runs(session_id)
            WHERE state IN ('starting','running','stopping');
        -- Conversations sharing one account may run concurrently; the former
        -- per-account active-run index is dropped from existing stores.
        DROP INDEX IF EXISTS active_account;
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL, session_id TEXT NOT NULL, kind TEXT NOT NULL,
            at REAL NOT NULL, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS run_route_exclusions(
            run_id TEXT PRIMARY KEY, refs TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS event_run ON events(run_id,seq);
        CREATE INDEX IF NOT EXISTS event_session ON events(session_id,seq);
        CREATE TABLE IF NOT EXISTS account_observations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL, observed_at REAL NOT NULL,
            source TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS account_observation_latest
            ON account_observations(account_id, observed_at DESC, id DESC);
        CREATE TABLE IF NOT EXISTS usage_observations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL, observed_at REAL NOT NULL,
            source TEXT NOT NULL, scope TEXT NOT NULL,
            stale INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS usage_observation_latest
            ON usage_observations(account_id, scope, observed_at DESC, id DESC);
        CREATE TABLE IF NOT EXISTS instance_requests(
            request_key TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE,
            payload TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS auth_attempts(
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, account_id TEXT NOT NULL,
            engine TEXT NOT NULL, name TEXT NOT NULL, email TEXT,
            mode TEXT NOT NULL, browser TEXT NOT NULL, request_key TEXT,
            grantbridge_id TEXT, status TEXT NOT NULL, data TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS auth_owner_request
            ON auth_attempts(owner, request_key)
            WHERE request_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS auth_proxy_routes(
            attempt_id TEXT PRIMARY KEY, config TEXT NOT NULL,
            connection TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS retired_accounts(
            account_id TEXT PRIMARY KEY, retired_at REAL NOT NULL);
    ''')
    version = db.execute('SELECT version FROM metadata').fetchone()[0]
    if version == 1:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS account_observations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL, observed_at REAL NOT NULL,
                source TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS account_observation_latest
                ON account_observations(account_id, observed_at DESC, id DESC);
            CREATE TABLE IF NOT EXISTS usage_observations(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id TEXT NOT NULL, observed_at REAL NOT NULL,
                source TEXT NOT NULL, scope TEXT NOT NULL,
                stale INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS usage_observation_latest
                ON usage_observations(account_id, scope, observed_at DESC, id DESC);
            UPDATE metadata SET version=2;
        ''')
        version = 2
    if version < 3:
        columns = {row['name'] for row in db.execute('PRAGMA table_info(runs)')}
        if 'message_id' not in columns:
            db.execute('ALTER TABLE runs ADD COLUMN message_id TEXT')
            db.execute('UPDATE runs SET message_id=id WHERE message_id IS NULL')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS run_message_id ON runs(message_id)')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS instance_requests(
                request_key TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS auth_attempts(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, account_id TEXT NOT NULL,
                engine TEXT NOT NULL, name TEXT NOT NULL, email TEXT,
                mode TEXT NOT NULL, browser TEXT NOT NULL, request_key TEXT,
                grantbridge_id TEXT, status TEXT NOT NULL, data TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS auth_owner_request
                ON auth_attempts(owner, request_key)
                WHERE request_key IS NOT NULL;
            UPDATE metadata SET version=3;
        ''')
        version = 3
    version = migrate_v4(db, version)
    version = migrate_v5(db, version)
    version = migrate_v6(db, version)
    version = migrate_v7(db, version)
    version = migrate_v8(db, version)
    version = migrate_v9(db, version)
    version = migrate_v10(db, version)
    version = migrate_v11(db, version)
    version = migrate_v12(db, version)
    version = migrate_v13(db, version)
    version = migrate_v14(db, version)
    version = migrate_v15(db, version)
    version = migrate_v16(db, version)
    if version != 16:
        raise BridgeError("schema_version", "This store needs a different AgentBridge version.")
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS run_message_id ON runs(message_id)')


def initialize_optional(store):
    """Make the complete table inventory visible before host writer instrumentation."""
    from ..error_diagnosis import _initialize as diagnoses
    from ..error_learning import Learning
    from ..permissions import Permissions
    from ..provider_contracts import ContractRegistry
    from ..proxy.credential_barrier import initialize as credentials

    diagnoses(store)
    Permissions(store)
    Learning(store)
    ContractRegistry(store)
    with store.connect() as connection:
        credentials(connection)
