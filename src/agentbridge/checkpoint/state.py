"""Persistent Store identity, admission fences and generation-scoped cursors."""

import json
import re
from uuid import UUID, uuid4

from ..errors import BridgeError

ACTIVE = ('starting', 'running', 'stopping')
IDENTITY_KEYS = ('owner_ref', 'store_id', 'store_generation')


def canonical_uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise BridgeError('checkpoint_invalid', 'A canonical UUID is required.') from None
    return value


def migrate(db, version):
    if version != 13:
        return version
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    current = db.execute('SELECT version FROM metadata').fetchone()[0]
    if current != 13:
        return current
    db.execute('''CREATE TABLE IF NOT EXISTS store_identity(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        store_id TEXT NOT NULL, owner_ref TEXT NOT NULL, store_generation TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0, recovery_held INTEGER NOT NULL DEFAULT 0,
        source_generation TEXT, source_seq INTEGER NOT NULL DEFAULT 0)''')
    store_id, generation = str(uuid4()), str(uuid4())
    db.execute('INSERT OR IGNORE INTO store_identity '
               '(singleton,store_id,owner_ref,store_generation,enabled,recovery_held,'
               'source_generation,source_seq) VALUES (1,?,?,?,0,0,NULL,0)',
               (store_id, 'owner-' + store_id, generation))
    db.execute('''CREATE TABLE IF NOT EXISTS native_checkpoints(
        turn_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
        barrier_id TEXT NOT NULL UNIQUE, checkpoint_id TEXT NOT NULL UNIQUE,
        operation_id TEXT NOT NULL UNIQUE, generation TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','sealed_local','ready')),
        execution_seq INTEGER NOT NULL, terminal_seq INTEGER, observation_seq INTEGER,
        process_verified INTEGER NOT NULL DEFAULT 0, terminal_intent TEXT NOT NULL,
        source_fingerprint TEXT, descriptor TEXT, error_code TEXT, created_at TEXT NOT NULL, sealed_at TEXT, quiescence_ref TEXT)''')
    db.execute('CREATE INDEX IF NOT EXISTS checkpoint_instance '
               'ON native_checkpoints(instance_id,state)')
    db.execute('''CREATE TABLE IF NOT EXISTS checkpoint_restore_operations(
        operation_id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL,
        instance_id TEXT NOT NULL, checkpoint_id TEXT NOT NULL, result TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS store_snapshot_operations(
        operation_id TEXT PRIMARY KEY, generation TEXT NOT NULL, created_at TEXT NOT NULL,
        descriptor TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS recovery_workspaces(
        instance_id TEXT PRIMARY KEY, source_path TEXT NOT NULL, target_path TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS continuity_holds(
        instance_id TEXT PRIMARY KEY, reason TEXT NOT NULL)''')
    db.execute('UPDATE metadata SET version=14')
    return 14


def identity(db):
    row = db.execute('SELECT * FROM store_identity WHERE singleton=1').fetchone()
    return dict(row)


def migrate_mode(db, version):
    if version != 15:
        return version
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    current = db.execute('SELECT version FROM metadata').fetchone()[0]
    if current != 15:
        return current
    columns = {row['name'] for row in db.execute('PRAGMA table_info(store_identity)')}
    if 'checkpoint_mode' not in columns:
        db.execute("ALTER TABLE store_identity ADD COLUMN checkpoint_mode TEXT NOT NULL "
                   "DEFAULT 'required' CHECK(checkpoint_mode IN ('required','on_demand'))")
    db.execute('UPDATE metadata SET version=16')
    return 16


def configure(store, *, owner_ref=None, durable=None, checkpoint_mode=None):
    """Local-host configuration, never accepted from an administrative RPC request body."""
    if owner_ref is not None and (not isinstance(owner_ref, str)
                                 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', owner_ref)):
        raise BridgeError('checkpoint_owner_invalid', 'Choose a stable private owner reference.')
    if durable is not None and type(durable) is not bool:
        raise BridgeError('checkpoint_invalid', 'Durable mode must be a boolean.')
    if checkpoint_mode is not None and checkpoint_mode not in ('required', 'on_demand'):
        raise BridgeError('checkpoint_invalid', 'Choose required or on_demand checkpoint capture.')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = identity(db)
        changed = owner_ref is not None and owner_ref != current['owner_ref']
        if changed and current['enabled']:
            raise BridgeError('checkpoint_owner_mismatch', 'This Store belongs to another owner.')
        if current['enabled'] and durable is False:
            raise BridgeError('checkpoint_required', 'An enabled durable Store cannot downgrade.')
        mode_changed = checkpoint_mode is not None and checkpoint_mode != current['checkpoint_mode']
        if checkpoint_mode == 'on_demand' and not (current['enabled'] or durable is True):
            raise BridgeError('checkpoint_disabled', 'On-demand capture requires durable identity.')
        if changed or mode_changed or durable is True and not current['enabled']:
            active = db.execute(
                "SELECT 1 FROM runs WHERE state IN ('starting','running','stopping')").fetchone()
            if active:
                raise BridgeError('checkpoint_busy', 'Drain execution before configuring durability.')
        if changed:
            db.execute('UPDATE store_identity SET owner_ref=? WHERE singleton=1', (owner_ref,))
        if durable is True:
            db.execute('UPDATE store_identity SET enabled=1 WHERE singleton=1')
        if mode_changed:
            db.execute('UPDATE store_identity SET checkpoint_mode=? WHERE singleton=1',
                       (checkpoint_mode,))


def recovery_held(db, instance_id):
    return bool(identity(db)['recovery_held'] or db.execute(
        'SELECT 1 FROM continuity_holds WHERE instance_id=?', (instance_id,)).fetchone())


def blocked(db, instance_id):
    from ..proxy.credential_barrier import hold

    session = db.execute('SELECT account_id FROM sessions WHERE id=?', (instance_id,)).fetchone()
    if session and hold(db, session[0]):
        return True
    if recovery_held(db, instance_id):
        return True
    binding = identity(db)
    return binding['enabled'] and db.execute(
        "SELECT 1 FROM native_checkpoints WHERE instance_id=? AND state<>'ready' "
        "AND (?='required' OR process_verified=0)",
        (instance_id, binding['checkpoint_mode'])).fetchone() is not None


def require_admission(db, instance_id):
    from ..proxy.credential_barrier import require_account

    session = db.execute('SELECT account_id FROM sessions WHERE id=?', (instance_id,)).fetchone()
    if session:
        require_account(db, session[0])
    if blocked(db, instance_id):
        raise BridgeError('checkpoint_pending', 'Native continuity is held until recovery completes.',
                          retryable=True, details={'instance_id': instance_id})


def maximum_seq(db):
    return db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]


def cursor(db, seq=None):
    value = identity(db)
    return {'format_version': '1', **{key: value[key] for key in IDENTITY_KEYS},
            'seq': maximum_seq(db) if seq is None else seq}


def require_cursor(db, supplied):
    expected = cursor(db)
    if (not isinstance(supplied, dict) or set(supplied) != set(expected)
            or supplied.get('format_version') != '1'
            or any(supplied.get(key) != expected[key] for key in IDENTITY_KEYS)
            or type(supplied.get('seq')) is not int or supplied['seq'] < 0):
        raise BridgeError('cursor_generation_mismatch', 'Cursor belongs to another Store generation.',
                          details={'current_cursor': expected})
    imported = identity(db)
    if supplied['seq'] < imported['source_seq']:
        raise BridgeError('cursor_generation_mismatch', 'Reconcile the imported history before replay.',
                          details={'replay_start': cursor(db, imported['source_seq'])})
    if supplied['seq'] > expected['seq']:
        raise BridgeError('cursor_rollback', 'Store history is behind the supplied cursor.',
                          details={'current_cursor': expected})
    return supplied['seq']


def saved_intent(store, turn_id):
    with store.connect() as db:
        row = db.execute('SELECT terminal_intent FROM native_checkpoints WHERE turn_id=?',
                         (turn_id,)).fetchone()
    return json.loads(row[0]) if row else None
