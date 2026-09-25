"""Observe real filesystem access through both native transport wrappers."""

import json
from pathlib import Path
import sys


def send(value):
    print(json.dumps(value), flush=True)


def inspect(prompt):
    paths = json.loads(prompt)
    observed = {}
    for label, path in paths.items():
        target = Path(path)
        try:
            target.read_text()
            observed[label + '_read'] = True
        except OSError:
            observed[label + '_read'] = False
        try:
            target.write_text('synthetic access probe')
            observed[label + '_write'] = True
        except OSError:
            observed[label + '_write'] = False
    return json.dumps(observed)


def main():
    if 'app-server' not in sys.argv:
        send({'type': 'thread.started', 'thread_id': 'access-fixture'})
        send({'type': 'item.completed', 'item': {
            'type': 'agent_message', 'text': inspect(sys.stdin.read())}})
        send({'type': 'turn.completed'})
        return
    for line in sys.stdin:
        request = json.loads(line)
        method = request['method']
        if method == 'initialize':
            send({'id': request['id'], 'result': {}})
        elif method in ('thread/start', 'thread/resume'):
            send({'id': request['id'], 'result': {'thread': {'id': 'access-fixture'}}})
        elif method == 'turn/start':
            send({'id': request['id'], 'result': {'turn': {'id': 'access-turn'}}})
            send({'method': 'item/completed', 'params': {'threadId': 'access-fixture',
                  'item': {'type': 'agentMessage', 'id': 'answer',
                           'text': inspect(request['params']['input'][0]['text'])}}})
            send({'method': 'turn/completed', 'params': {'threadId': 'access-fixture',
                  'turn': {'id': 'access-turn', 'status': 'completed'}}})
            return


if __name__ == '__main__':
    main()
