"""Page transcript messages before reconstructing their provider observations."""
import json
import math
from .errors import BridgeError, UnsupportedError
from .models import page_values


def messages(bridge, instance_id, *, after=None, before=None, role=None, limit=100, cursor=0):
    limit, cursor = page_values(limit, cursor)
    if before is not None:
        raise UnsupportedError('before filtering is not supported by this adapter.')
    if role not in (None, 'user', 'assistant'):
        raise BridgeError('invalid_role', 'role must be user or assistant.')
    if after is not None and (not isinstance(after, (int, float)) or isinstance(after, bool) or not math.isfinite(after)):
        raise BridgeError('invalid_params', 'after must be an epoch timestamp.')
    bridge.get_session(instance_id)
    # The cursor is a message offset, not a run rowid. Only selected assistant
    # messages need event reconstruction. Live clients should use instances.events.
    with bridge.store.connect() as db:
        rows = db.execute('''WITH transcript AS (
            SELECT rowid AS position, 0 AS slot, id, message_id, prompt AS content,
                   created AS at, 'user' AS role,
                   (SELECT json_extract(data,'$.attachments') FROM events
                    WHERE run_id=runs.id AND kind='user' ORDER BY seq LIMIT 1) AS attachments
                   FROM runs WHERE session_id=?
            UNION ALL
            SELECT rowid AS position, 1 AS slot, id, message_id, NULL AS content,
                   updated AS at, 'assistant' AS role, NULL AS attachments FROM runs WHERE session_id=?
              AND EXISTS (SELECT 1 FROM events WHERE run_id=runs.id
                AND kind IN ('assistant','text_delta') AND json_extract(data,'$.text') <> '')
        ) SELECT * FROM transcript WHERE (? IS NULL OR role=?) AND (? IS NULL OR at>?)
          ORDER BY position,slot LIMIT ? OFFSET ?''',
                          (instance_id, instance_id, role, role, after, after, limit, cursor)).fetchall()
    result = []
    for row in rows:
        projection = bridge.run(row['id']).message if row['role'] == 'assistant' else None
        value = {'message_id': row['message_id'] + (':assistant' if row['role'] == 'assistant' else ''),
                 'instance_id': instance_id, 'role': row['role'],
                 'content': row['content'] if projection is None else projection['text'],
                 'sequence': row['id'], 'created_at': row['at'],
                 'attachments': json.loads(row['attachments'] or '[]')}
        if projection is not None:
            value.update({key: projection[key] for key in ('incomplete', 'retracted', 'retracted_provider_message_ids')})
        result.append(value)
    return result
