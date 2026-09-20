"""Deterministic, isolated authentication subprocess. Never contacts a provider."""
import json
from pathlib import Path
import sys
import time

def invoke(root, method, params):
    state = root / 'attempt.json'
    if method == 'auth.close':
        return {'closed': True}
    if method == 'auth.start':
        row = {'id': 'fixture-attempt', 'status': 'awaiting_user', 'provider': params['engine'],
               'owner': params['owner'], 'key': params.get('request_key'),
               'identity': {'email': 'fixture@example.test'},
               'authorizationUrl': 'https://provider.example.test/login'}
        if state.exists():
            row = json.loads(state.read_text())
        else:
            state.write_text(json.dumps(row))
        return row
    row = json.loads(state.read_text())
    if row['owner'] != params.get('owner'):
        raise ValueError('not_found')
    if method == 'auth.cancel':
        row['status'] = 'cancelled'
    elif method == 'auth.get' and (root / 'authorize').exists() and row['status'] == 'awaiting_user':
        row['status'] = 'authorized'
    elif method == 'auth.check':
        time.sleep(0.3)
        row['verification'] = {'freshProcess': 'passed'}
    elif method == 'auth.activate':
        if (root / 'expired').exists():
            raise ValueError('credential_expired')
        if row.get('verification', {}).get('freshProcess') != 'passed':
            raise ValueError('authentication_not_verified')
        return {**row, 'attempt_id': row['id'], 'credential_ref': {'provider': row['provider'], 'attempt_id': row['id']},
                'home': str(root / 'native') if row['provider'] != 'cursor' else None}
    elif method == 'auth.credentials':
        if (root / 'expired').exists():
            raise ValueError('credential_expired')
        return {'provider': 'cursor', 'api_key': 'fixture-key-never-real'}
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
