"""Durable routing metadata for v2 sessions."""


def migrate_v4(db, version):
    if version != 3:
        return version
    db.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS session_routing(
            session_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL CHECK(mode IN ('pinned','automatic')),
            last_completed_account_id TEXT,
            last_native_id TEXT);
        INSERT OR IGNORE INTO session_routing(session_id,mode,last_completed_account_id,last_native_id)
            SELECT id,'pinned',account_id,native_id FROM sessions;
        UPDATE metadata SET version=4;
        COMMIT;
    ''')
    return 4


def migrate_v5(db, version):
    if version != 4:
        return version
    db.executescript('''
        BEGIN IMMEDIATE;
        ALTER TABLE session_routing ADD COLUMN provider TEXT;
        UPDATE metadata SET version=5;
        COMMIT;
    ''')
    return 5


def migrate_v11(db, version):
    if version != 10:
        return version
    db.executescript('''
        BEGIN IMMEDIATE;
        ALTER TABLE session_routing ADD COLUMN affinity_account_id TEXT;
        UPDATE session_routing
           SET affinity_account_id = COALESCE(
               (SELECT runs.account_id FROM runs
                 WHERE runs.session_id = session_routing.session_id
                 ORDER BY runs.rowid DESC LIMIT 1),
               (SELECT sessions.account_id FROM sessions
                 WHERE sessions.id = session_routing.session_id))
         WHERE mode = 'automatic';
        UPDATE metadata SET version=11;
        COMMIT;
    ''')
    return 11
