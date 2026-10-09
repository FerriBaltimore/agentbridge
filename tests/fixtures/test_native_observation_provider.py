"""Native protocol subprocess with history independent of AgentBridge projections."""

import json
import os
from pathlib import Path
import sys


def send(value):
    print(json.dumps(value), flush=True)


def main():
    path = Path(os.environ['CODEX_HOME']) / 'fixture-native-thread.json'
    native = json.loads(path.read_text()) if path.exists() else None
    active = None
    read_advanced = False

    def save():
        path.write_text(json.dumps(native))

    def notice(method, **params):
        send({'method': method, 'params': {'threadId': 'native-thread', **params}})

    def finish(state):
        nonlocal active
        active['status'] = state
        active['items'][1]['text'] = 'partial answer complete'
        active['items'][2]['status'] = 'completed'
        notice('item/completed', turnId=active['id'], item=active['items'][1])
        notice('item/completed', turnId=active['id'], item=active['items'][2])
        native['status'] = {'type': 'idle'}
        save()
        notice('turn/completed', turn=active)
        notice('thread/status/changed', status=native['status'])
        active = None

    for line in sys.stdin:
        request = json.loads(line)
        method, params = request['method'], request.get('params', {})
        result = {}
        if method == 'initialized':
            continue
        if method == 'thread/start':
            assert native is None
            native = {'id': 'native-thread', 'status': {'type': 'idle'}, 'turns': []}
            save()
            result = {'thread': native}
        elif method in {'thread/resume', 'thread/read'}:
            if native is None:
                send({'id': request['id'], 'error': {'code': -32600,
                    'message': ('no rollout found for thread id ' if method == 'thread/resume'
                                else 'thread not loaded: ') + params['threadId']}})
                continue
            assert params['threadId'] == 'native-thread'
            if method == 'thread/read' and active and not read_advanced:
                read_advanced = True
                active['items'][1]['text'] += ' during read'
                notice('item/agentMessage/delta', turnId=active['id'], itemId='answer',
                       delta=' during read')
                save()
            result = {'thread': native if params.get('includeTurns', True) else
                      {key: value for key, value in native.items() if key != 'turns'}}
        elif method == 'turn/start':
            active = {'id': 'native-turn-' + str(len(native['turns']) + 1),
                      'status': 'inProgress', 'items': [
                          {'id': 'user', 'type': 'userMessage', 'content': params['input']},
                          {'id': 'answer', 'type': 'agentMessage', 'phase': 'commentary',
                           'text': 'partial answer'},
                          {'id': 'tool', 'type': 'commandExecution', 'command': 'fixture command',
                           'status': 'inProgress', 'aggregatedOutput': 'partial output'},
                          {'id': 'native-only', 'type': 'agentMessage', 'text': 'Native history only'},
                          {'id': 'private', 'type': 'reasoning', 'text': 'never expose'}]}
            native['turns'].append(active)
            native['status'] = {'type': 'active', 'activeFlags': []}
            save()
            send({'id': request['id'], 'result': {'turn': active}})
            notice('thread/status/changed', status=native['status'])
            notice('turn/started', turn=active)
            notice('item/started', turnId=active['id'],
                   item={**active['items'][1], 'text': ''})
            notice('item/agentMessage/delta', turnId=active['id'], itemId='answer',
                   delta='partial answer')
            notice('item/started', turnId=active['id'],
                   item={**active['items'][2], 'aggregatedOutput': ''})
            notice('item/commandExecution/outputDelta', turnId=active['id'], itemId='tool',
                   delta='partial output')
            continue
        elif method == 'turn/steer':
            assert params['expectedTurnId'] == active['id']
            result = {'turnId': active['id']}
            send({'id': request['id'], 'result': result})
            finish('completed')
            continue
        elif method == 'turn/interrupt':
            assert params['turnId'] == active['id']
            send({'id': request['id'], 'result': result})
            finish('interrupted')
            continue
        send({'id': request['id'], 'result': result})


if __name__ == '__main__':
    main()
