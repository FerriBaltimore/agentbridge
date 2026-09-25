"""Bind each conversation once to its native Codex history."""

from .errors import BridgeError


def validate_native_id(native_id):
    if (not isinstance(native_id, str) or not native_id.strip()
            or len(native_id) > 512 or any(ord(char) < 32 for char in native_id)):
        raise BridgeError('native_session_missing',
                          'Codex did not identify the native conversation.',
                          phase='execution', outcome='unknown')
    return native_id


def bind_native_session(db, session_id, native_id):
    """Persist the first observation; reject a replacement in the same transaction."""
    validate_native_id(native_id)
    bound = db.execute('SELECT native_id FROM native_session_bindings WHERE session_id=?',
                       (session_id,)).fetchone()
    current = db.execute('SELECT native_id FROM sessions WHERE id=?',
                         (session_id,)).fetchone()
    if current is None:
        raise BridgeError('instance_not_found', 'Instance does not exist.')
    if ((bound is not None and bound['native_id'] != native_id)
            or (current['native_id'] is not None and current['native_id'] != native_id)):
        raise BridgeError('native_session_diverged',
                          'Codex returned a different native conversation for this instance.',
                          phase='execution', outcome='unknown')
    db.execute('INSERT OR IGNORE INTO native_session_bindings(session_id,native_id) VALUES (?,?)',
               (session_id, native_id))
    db.execute('UPDATE sessions SET native_id=? WHERE id=?', (native_id, session_id))


def require_native_session(db, session):
    """Permit a first start or an exact resume, never a replacement history."""
    native_id = session['native_id']
    bound = db.execute('SELECT native_id FROM native_session_bindings WHERE session_id=?',
                       (session['id'],)).fetchone()
    if bound is not None:
        validate_native_id(bound['native_id'])
        if native_id is None:
            raise BridgeError('native_session_missing',
                              'The bound native conversation is unavailable; a new thread cannot replace it.')
        if native_id != bound['native_id']:
            raise BridgeError('native_session_diverged',
                              'The instance no longer identifies its bound native conversation.')
        return validate_native_id(native_id)
    if native_id is not None:
        # Explicit historical imports bind on admission, before native execution.
        bind_native_session(db, session['id'], native_id)
        return native_id
    previous = db.execute("SELECT 1 FROM runs WHERE session_id=? "
                          "AND (state='completed' OR child_pid IS NOT NULL) "
                          "UNION ALL SELECT 1 FROM native_session_launches WHERE session_id=? "
                          "AND may_have_started=1 "
                          "UNION ALL SELECT 1 FROM events WHERE session_id=? "
                          "AND kind IN ('run_started','session','assistant','text_delta','tool_call') LIMIT 1",
                          (session['id'], session['id'], session['id'])).fetchone()
    if previous:
        raise BridgeError('native_session_missing',
                          'Previous execution has no native conversation identity; a new thread cannot replace it.')
    return None


def mark_native_launch(store, run_id, *, not_started=False):
    """Persist launch intent before Popen; only a definite launch failure clears it."""
    with store.connect() as db:
        db.execute('INSERT INTO native_session_launches(run_id,session_id,may_have_started) '
                   'SELECT id,session_id,? FROM runs WHERE id=? '
                   'ON CONFLICT(run_id) DO UPDATE SET may_have_started=excluded.may_have_started',
                   (int(not not_started), run_id))


def migrate_v12(db, version):
    """Anchor existing chats to their current native history without creating threads."""
    if version != 11:
        return version
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    current = db.execute('SELECT version FROM metadata').fetchone()[0]
    if current != 11:
        return current
    db.execute('''CREATE TABLE IF NOT EXISTS native_session_bindings(
            session_id TEXT PRIMARY KEY,
            native_id TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS native_session_launches(
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            may_have_started INTEGER NOT NULL CHECK(may_have_started IN (0,1)))''')
    db.execute('''INSERT OR IGNORE INTO native_session_bindings(session_id,native_id)
            SELECT id, native_id FROM sessions WHERE native_id IS NOT NULL''')
    db.execute('''INSERT OR IGNORE INTO native_session_bindings(session_id,native_id)
            SELECT s.id, COALESCE(
                (SELECT json_extract(e.data,'$.native_id') FROM events e
                 WHERE e.session_id=s.id AND e.kind='session'
                   AND json_type(e.data,'$.native_id')='text'
                   AND length(json_extract(e.data,'$.native_id'))>0
                 ORDER BY e.seq DESC LIMIT 1), r.last_native_id)
            FROM sessions s JOIN session_routing r ON r.session_id=s.id
            WHERE s.native_id IS NULL AND (r.last_native_id IS NOT NULL OR EXISTS (
                SELECT 1 FROM events e WHERE e.session_id=s.id AND e.kind='session'
                AND json_type(e.data,'$.native_id')='text'
                AND length(json_extract(e.data,'$.native_id'))>0))''')
    db.execute('''UPDATE sessions SET native_id=(
            SELECT b.native_id FROM native_session_bindings b WHERE b.session_id=sessions.id)
            WHERE native_id IS NULL''')
    db.execute('UPDATE metadata SET version=12')
    return 12
