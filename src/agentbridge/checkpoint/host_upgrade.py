"""Private inherited-pipe host driver for a quiesced Store; this is not the model RPC API.

The launching host owns both pipes and supplies live scope-verification callbacks. A proof
reference, request field or successful process launch never substitutes for those callbacks.
"""

import argparse
import json
import sys
from uuid import uuid4

from ..client import Bridge
from ..errors import BridgeError
from . import persistence, state

MAX_LINE = 4 * 1024 * 1024


def read():
    line = sys.stdin.buffer.readline(MAX_LINE + 1)
    if not line:
        raise EOFError()
    if len(line) > MAX_LINE or not line.endswith(b'\n'):
        raise ValueError('invalid_frame')
    return json.loads(line)


def write(value):
    line = json.dumps(value, separators=(',', ':'), allow_nan=False)
    if len(line.encode()) > MAX_LINE:
        raise ValueError('invalid_frame')
    print(line, flush=True)


def verifier(request_id, kind):
    def verify(scope):
        nonce = str(uuid4())
        write({'id': request_id, 'verify': kind, 'nonce': nonce, 'scope': scope})
        answer = read()
        return (isinstance(answer, dict) and set(answer) == {'id', 'nonce', 'verified'}
                and answer['id'] == request_id and answer['nonce'] == nonce
                and answer['verified'] is True)
    return verify


def dispatch(bridge, request):
    if not isinstance(request, dict) or set(request) != {'id', 'action', 'params'}:
        raise ValueError('invalid_request')
    request_id = state.canonical_uuid(request['id'])
    params = request['params']
    if not isinstance(params, dict):
        raise ValueError('invalid_request')
    action = request['action']
    if action == 'identity' and not params:
        with bridge.store.connect() as db:
            count = db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        return {**bridge.checkpoints.identity(), 'instance_count': count}
    if action == 'begin' and set(params) == {'operation_id', 'owner_ref', 'proof_ref'}:
        return bridge.checkpoints.begin_upgrade(**params,
            verify_quiescence=verifier(request_id, 'quiescence'))
    if action == 'stage' and set(params) == {'operation_id', 'instance_id', 'expected'}:
        return bridge.checkpoints.stage_upgrade(**params,
            verify_quiescence=verifier(request_id, 'quiescence'))
    if action == 'confirm' and set(params) == {
            'operation_id', 'instance_id', 'reconciliation_ref'}:
        return bridge.checkpoints.confirm_upgrade(**params,
            verify_reconciliation=verifier(request_id, 'reconciliation'))
    raise ValueError('invalid_request')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    with Bridge(args.root) as bridge:
        while True:
            request = None
            try:
                request = read()
                result = dispatch(bridge, request)
                write({'id': request['id'], 'result': result})
            except EOFError:
                return
            except Exception as error:
                code = persistence.safe_error(error.code if isinstance(error, BridgeError)
                                              else None)['code']
                write({'id': request.get('id') if isinstance(request, dict) else None,
                       'error': code})


if __name__ == '__main__':
    main()
