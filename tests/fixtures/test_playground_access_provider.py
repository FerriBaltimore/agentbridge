"""Codex-shaped access-policy fixture with persistent native history."""

import json
import os
from pathlib import Path
import sys


NATIVE_ID = 'fixture-access-session'


def send(value):
    print(json.dumps(value), flush=True)


def record(prompt, *, resumed, sandbox, approval, model):
    home = Path(os.environ['CODEX_HOME'])
    history_file = home / 'fixture-access-history.json'
    if resumed:
        history = json.loads(history_file.read_text())
    else:
        assert not history_file.exists(), 'The chat started a replacement native session.'
        history = []
    evidence = {'argv': sys.argv[1:], 'prompt': prompt, 'resumed': resumed,
                'native_id': NATIVE_ID, 'sandbox': sandbox, 'approval': approval,
                'model': model, 'previous_prompts': list(history)}
    with (home / 'fixture-calls.jsonl').open('a') as stream:
        stream.write(json.dumps(evidence) + '\n')
    history_file.write_text(json.dumps([*history, prompt]))


def exec_turn():
    prompt = sys.stdin.read()
    resumed = 'resume' in sys.argv
    if resumed:
        assert sys.argv[sys.argv.index('resume') + 1] == NATIVE_ID
    config = {}
    for index, argument in enumerate(sys.argv[:-1]):
        if argument == '-c':
            key, value = sys.argv[index + 1].split('=', 1)
            config[key] = json.loads(value)
    record(prompt, resumed=resumed, sandbox=config['sandbox_mode'],
           approval=config['approval_policy'], model=sys.argv[sys.argv.index('--model') + 1])
    send({'type': 'thread.started', 'thread_id': NATIVE_ID})
    send({'type': 'item.completed', 'item': {'type': 'agent_message',
          'text': 'Access fixture answer'}})
    send({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}})


def app_server_turn():
    while True:
        request = json.loads(sys.stdin.readline())
        if request['method'] == 'initialize':
            send({'id': request['id'], 'result': {}})
        elif request['method'] == 'initialized':
            continue
        elif request['method'] in ('thread/start', 'thread/resume'):
            resumed = request['method'] == 'thread/resume'
            params = request['params']
            if resumed:
                assert params['threadId'] == NATIVE_ID
            send({'id': request['id'], 'result': {'thread': {'id': NATIVE_ID}}})
        elif request['method'] == 'turn/start':
            assert request['params']['threadId'] == NATIVE_ID
            break
    record(request['params']['input'][0]['text'], resumed=resumed,
           sandbox=params['sandbox'], approval=params['approvalPolicy'], model=params['model'])
    send({'id': request['id'], 'result': {'turn': {'id': 'fixture-turn'}}})
    send({'method': 'item/completed', 'params': {'threadId': NATIVE_ID,
          'turnId': 'fixture-turn', 'item': {'type': 'agentMessage', 'id': 'answer',
          'text': 'Access fixture answer'}}})
    send({'method': 'turn/completed', 'params': {'threadId': NATIVE_ID,
          'turn': {'id': 'fixture-turn', 'status': 'completed'}}})


if __name__ == '__main__':
    if '--version' in sys.argv:
        print('codex-cli 0.0.0')
    elif 'app-server' in sys.argv:
        app_server_turn()
    else:
        exec_turn()
