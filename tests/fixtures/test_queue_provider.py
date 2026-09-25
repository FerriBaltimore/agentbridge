"""Deterministic Codex-shaped subprocess for queue and steering acceptance."""

import json
from pathlib import Path
import select
import sys


def send(value):
    print(json.dumps(value), flush=True)


def main():
    while True:
        request = json.loads(sys.stdin.readline())
        method = request['method']
        if method == 'initialized':
            continue
        if method == 'initialize':
            send({'id': request['id'], 'result': {}})
        elif method in ('thread/start', 'thread/resume'):
            send({'id': request['id'], 'result': {'thread': {'id': 'fixture-thread'}}})
        elif method == 'turn/start':
            break
    prompt = request['params']['input'][0]['text']
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
