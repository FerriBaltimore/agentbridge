"""Deterministic Codex-shaped subprocess for queue and steering acceptance."""

import json
import os
from pathlib import Path
import select
import sys


def send(value):
    print(json.dumps(value), flush=True)


def main():
    history_path = Path(os.environ['CODEX_HOME']) / 'fixture-queue-native.json'
    while True:
        request = json.loads(sys.stdin.readline())
        method = request['method']
        if method == 'initialized':
            continue
        if method == 'initialize':
            send({'id': request['id'], 'result': {}})
        elif method in ('thread/start', 'thread/resume'):
            if method == 'thread/start':
                assert not history_path.exists(), 'A queued turn replaced its Codex session.'
                history = {'methods': [], 'prompts': []}
            else:
                assert request['params']['threadId'] == 'fixture-thread'
                history = json.loads(history_path.read_text())
            history['methods'].append(method)
            history_path.write_text(json.dumps(history))
            send({'id': request['id'], 'result': {'thread': {'id': 'fixture-thread'}}})
        elif method == 'turn/start':
            break
    prompt = request['params']['input'][0]['text']
    history['prompts'].append(prompt)
    history_path.write_text(json.dumps(history))
    send({'id': request['id'], 'result': {'turn': {'id': 'fixture-turn'}}})
    send({'method': 'item/agentMessage/delta', 'params': {
        'threadId': 'fixture-thread', 'turnId': 'fixture-turn', 'delta': 'ready'}})
    additions = []
    while prompt.startswith('hold:') and not Path(prompt.split(':', 1)[1]).exists():
        if not select.select([sys.stdin], [], [], .02)[0]:
            continue
        steer = json.loads(sys.stdin.readline())
        assert steer['method'] == 'turn/steer'
        assert steer['params']['expectedTurnId'] == 'fixture-turn'
        text = steer['params']['input'][0]['text']
        if text == 'drop':
            return
        if text == 'complete_without_ack':
            break
        if text == 'reject':
            send({'id': steer['id'], 'error': {'code': -32600, 'message': 'Synthetic rejection'}})
            continue
        additions.append(text)
        send({'id': steer['id'], 'result': {'turnId': 'fixture-turn'}})
        if text == 'finish':
            break
    send({'method': 'item/completed', 'params': {'threadId': 'fixture-thread',
          'turnId': 'fixture-turn', 'item': {'type': 'agentMessage', 'id': 'answer',
          'text': prompt + '|steer=' + ','.join(additions)}}})
    send({'method': 'turn/completed', 'params': {'threadId': 'fixture-thread',
          'turn': {'id': 'fixture-turn', 'status': 'failed' if prompt == 'fail' else 'completed'}}})


if __name__ == '__main__':
    main()
