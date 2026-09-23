"""Deterministic GrantBridge proxy adapter; it never contacts a provider."""

import json
from pathlib import Path
import sys


def invoke(root, method, params):
    state = root / 'proxy-attempt.json'
    if method == 'auth.close':
        return {'closed': True}
    if method == 'auth.proxy_start':
        if state.exists():
            raise ValueError('provider_busy')
        row = {'id': 'fixture-proxy-state', 'provider': params['provider'],
               'status': 'awaiting_user',
               'authorizationUrl': 'https://provider.example.test/login'}
        state.write_text(json.dumps(row))
        return row
    if method not in ('auth.proxy_status', 'auth.proxy_cancel') or not state.exists():
        raise ValueError('not_found')
    row = json.loads(state.read_text())
    if params['state'] != row['id'] or params['provider'] != row['provider']:
        raise ValueError('not_found')
    if method == 'auth.proxy_cancel':
        row['status'] = 'cancelled'
    elif (root / 'authorize').exists() and row['status'] == 'awaiting_user':
        row['status'] = 'authorized'
    state.write_text(json.dumps(row))
    return row


def main():
    root = Path(sys.argv[sys.argv.index('--data-dir') + 1])
    root.mkdir(parents=True, exist_ok=True)
    for line in sys.stdin:
        request = json.loads(line)
        try:
            response = {'result': invoke(root, request['method'], request.get('params', {}))}
        except ValueError as error:
            response = {'error': {'code': -32000, 'data': {'code': str(error)}}}
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], **response}), flush=True)


if __name__ == '__main__':
    main()
