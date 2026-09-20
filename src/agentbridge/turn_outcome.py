"""Read persisted terminal failure evidence for SDK and completion events."""
import json

from .provider_errors import normalize


def detail(db, turn_id, state, code):
    if state in {'completed', 'cancelled'}:
        return None
    row = db.execute("""SELECT data FROM events WHERE run_id=? AND kind='error'
        AND json_extract(data,'$.terminal')=1 ORDER BY seq DESC LIMIT 1""", (turn_id,)).fetchone()
    if row:
        return json.loads(row['data'])
    if code and state != 'cancelled':
        outcome = 'unknown' if state in {'interrupted', 'incomplete'} else 'failed'
        return normalize(None, {'code': code}, outcome=outcome)
    return None


def completion(db, turn_id, state, code, exit_code):
    issue = detail(db, turn_id, state, code)
    return {'state': state, 'code': code, 'exit_code': exit_code,
            'outcome': issue['outcome'] if issue else state, 'error_detail': issue}
