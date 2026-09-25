"""Stateful Codex subprocess: a second start cannot replace native history."""

import json
import os
from pathlib import Path
import sys
import time


def send(value):
    print(json.dumps(value), flush=True)


def bind_native(native_id, account):
    path = Path(os.environ['CODEX_HOME']) / 'fixture-native-session.json'
    if native_id:
        assert path.is_file(), 'Native history is unavailable in this Codex home.'
        state = json.loads(path.read_text())
        assert native_id == state['native_id'], 'Resumed a different Codex session.'
    else:
        assert not path.exists(), 'Started a replacement Codex session.'
        state = {'native_id': 'native-' + account, 'history': []}
        path.write_text(json.dumps(state))
    return path, state


def answer(path, state, prompt, *, account, resumed, model):
    result = {'account': account, 'resumed': resumed,
              'portable': prompt.startswith('Historical conversation evidence'),
              'model': model, 'native_id': state['native_id'],
              'previous_prompts': list(state['history'])}
    state['history'].append(prompt)
    path.write_text(json.dumps(state))
    return json.dumps(result)


def finish_state(prompt):
    if prompt.startswith('hold:'):
        marker = Path(prompt.split(':', 1)[1])
        while not marker.exists():
            time.sleep(.02)
    return {'fail': 'failed', 'interrupt': 'interrupted'}.get(prompt, 'completed')


def exec_turn(account):
    prompt = sys.stdin.read()
    native_id = sys.argv[sys.argv.index('resume') + 1] if 'resume' in sys.argv else None
    model = sys.argv[sys.argv.index('--model') + 1]
    path, state = bind_native(native_id, account)
    returned_id = ('replacement-native' if path.with_name('fixture-diverge').exists()
                   else state['native_id'])
    if not path.with_name('fixture-omit-native').exists():
        send({'type': 'thread.started', 'thread_id': returned_id})
    result = answer(path, state, prompt, account=account, resumed=bool(native_id), model=model)
    send({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': result}})
    status = finish_state(prompt)
    send({'type': 'turn.' + status, 'usage': {'input_tokens': 1, 'output_tokens': 1}})


def app_server_turn(account):
    while True:
        request = json.loads(sys.stdin.readline())
        method = request['method']
        if method == 'initialize':
            send({'id': request['id'], 'result': {}})
        elif method == 'initialized':
            continue
        elif method in ('thread/start', 'thread/resume'):
            resumed = method == 'thread/resume'
            native_id = request['params'].get('threadId')
            assert resumed == bool(native_id)
            model = request['params']['model']
            path, state = bind_native(native_id, account)
            send({'id': request['id'], 'result': {'thread': {'id': state['native_id']}}})
        elif method == 'turn/start':
            assert request['params']['threadId'] == state['native_id']
            break
    prompt = request['params']['input'][0]['text']
    result = answer(path, state, prompt, account=account, resumed=resumed, model=model)
    send({'id': request['id'], 'result': {'turn': {'id': 'fixture-turn'}}})
    send({'method': 'item/completed', 'params': {'threadId': state['native_id'],
          'turnId': 'fixture-turn', 'item': {'id': 'answer', 'type': 'agentMessage', 'text': result}}})
    status = finish_state(prompt)
    send({'method': 'turn/completed', 'params': {'threadId': state['native_id'],
          'turn': {'id': 'fixture-turn', 'status': status}}})


def main():
    account = 'alpha' if 'LAB_PROXY_ALPHA' in os.environ else 'beta'
    if 'app-server' in sys.argv:
        app_server_turn(account)
    else:
        exec_turn(account)


if __name__ == '__main__':
    main()
