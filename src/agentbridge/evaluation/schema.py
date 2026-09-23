"""Durable marker and minimal discard receipt for evaluation instances."""


def migrate_v6(db, version):
    if version != 5:
        return version
    db.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS evaluation_instances(
            session_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','discarding','discarded')),
            created REAL NOT NULL,
            discarded_at REAL);
        UPDATE metadata SET version=6;
        COMMIT;
    ''')
    return 6
