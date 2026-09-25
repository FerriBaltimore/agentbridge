"""Queue records contain input and credential references, never private bindings."""


def migrate_v10(db, version):
    if version != 9:
        return version
    db.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS conversation_queues(
            session_id TEXT PRIMARY KEY, version INTEGER NOT NULL DEFAULT 1,
            paused INTEGER NOT NULL DEFAULT 0, reason TEXT,
            dispatcher_pid INTEGER, dispatcher_identity TEXT);
        CREATE TABLE IF NOT EXISTS queued_messages(
            id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            content TEXT NOT NULL, options TEXT NOT NULL, exclusions TEXT NOT NULL,
            request_key TEXT UNIQUE, request_digest TEXT NOT NULL,
            position INTEGER NOT NULL, state TEXT NOT NULL,
            delivery TEXT NOT NULL DEFAULT 'queue', target_turn_id TEXT,
            turn_id TEXT, error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS queued_message_order
            ON queued_messages(session_id,state,position);
        UPDATE metadata SET version=10;
        COMMIT;
    ''')
    return 10
