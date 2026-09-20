"""Reconstruct visible text while retaining retracted observations in the log."""


def _ids(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

def retractions(events):
    by_run = {}
    for event in events:
        ids = (event.data.get('retracted_provider_message_ids') if event.kind == 'model_changed'
               else event.data.get('supersedes') if event.kind == 'assistant' else None)
        if not event.data.get('parent_id') and isinstance(ids, list):
            by_run.setdefault(event.run_id, set()).update(_ids(ids))
    return by_run


def text(events):
    messages = []
    pending = []
    retracted = set()
    terminal_state = None
    for event in events:
        data = event.data
        if event.kind == 'model_changed' and not data.get('parent_id'):
            retracted.update(_ids(data.get('retracted_provider_message_ids')))
            # A completed message already cleared its own deltas. Retraction
            # must not erase a newer, unfinished replacement response.
        elif event.kind == 'assistant' and not data.get('parent_id'):
            retracted.update(_ids(data.get('supersedes')))
            messages.append(data)
            pending.clear()
        elif event.kind == 'text_delta' and not data.get('parent_id'):
            pending.append(data.get('text', ''))
        elif event.kind == 'run_finished':
            terminal_state = data.get('state')
    visible = [item for item in messages if item.get('provider_message_id') not in retracted]
    completed = [item.get('text', '') for item in visible]
    # Deltas after the last complete block are useful during a continuing stream.
    content = '\n'.join(completed)
    if pending:
        content += ('\n' if content else '') + ''.join(pending)
    incomplete = (bool(pending) and terminal_state != 'completed'
                  or bool(content) and terminal_state not in {None, 'completed'}
                  or any(item.get('incomplete') for item in visible))
    return {'text': content, 'incomplete': incomplete,
            'retracted': bool(retracted), 'retracted_provider_message_ids': sorted(retracted)}
