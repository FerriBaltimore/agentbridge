"""Native app-server fixture shared by browser observation scenarios."""

import json
import os
from pathlib import Path
import sys


def send(value):
    print(json.dumps(value), flush=True)


def start():
    if '--version' in sys.argv:
        print('codex-cli 0.0.0')
        sys.exit(0)
    if 'app-server' not in sys.argv:
        prompt = sys.stdin.read()
        capture = Path(os.environ['CODEX_HOME']) / 'fixture-calls.jsonl'
        with capture.open('a') as stream:
            stream.write(json.dumps({'argv': sys.argv[1:], 'prompt': prompt}) + '\n')
        send({'type': 'thread.started', 'thread_id': 'fixture-native-session'})
        return prompt
    while True:
        request = json.loads(sys.stdin.readline())
        method = request['method']
        if method == 'initialize':
            send({'id': request['id'], 'result': {}})
        elif method == 'initialized':
            continue
        elif method in ('thread/start', 'thread/resume'):
            thread = request['params']
            resumed = method == 'thread/resume'
            if resumed:
                assert thread['threadId'] == 'fixture-native-session'
            send({'id': request['id'], 'result': {'thread': {'id': 'fixture-native-session'}}})
        elif method == 'turn/start':
            break
    params = request['params']
    prompt = params['input'][0]['text']
    capture = Path(os.environ['CODEX_HOME']) / 'fixture-calls.jsonl'
    with capture.open('a') as stream:
        stream.write(json.dumps({'argv': sys.argv[1:], 'prompt': prompt,
            'model': params.get('model', thread.get('model')), 'effort': params.get('effort'),
            'resumed': resumed, 'native_id': 'fixture-native-session'}) + '\n')
    send({'id': request['id'], 'result': {'turn': {'id': 'fixture-turn'}}})
    return prompt


def emit(value):
    """Express existing observation scenarios as native app-server notifications."""
    if 'app-server' not in sys.argv:
        send(value)
        return
    kind = value['type']
    params = {'threadId': 'fixture-native-session', 'turnId': 'fixture-turn'}
    if kind in ('thread.started', 'turn.started'):
        return
    if kind in ('turn.completed', 'turn.failed'):
        params['turn'] = {'id': 'fixture-turn', 'status': kind.split('.')[1]}
        if value.get('error'):
            params['turn']['error'] = value['error']
        method = 'turn/completed'
    elif kind in ('item.started', 'item.completed'):
        params['item'] = value['item'].copy()
        native = {'agent_message': 'agentMessage', 'command_execution': 'commandExecution',
                  'context_compaction': 'contextCompaction'}
        params['item']['type'] = native.get(params['item']['type'], params['item']['type'])
        if 'aggregated_output' in params['item']:
            params['item']['aggregatedOutput'] = params['item'].pop('aggregated_output')
        method = kind.replace('.', '/')
    elif kind == 'bridge_text_delta':
        method = 'item/agentMessage/delta'
        params['delta'] = value['text']
    else:
        method = 'fixture/unsupported'
    send({'method': method, 'params': params})
