"""The private host pipe requires live callbacks and cannot substitute a request proof flag."""

import json
import os
from pathlib import Path
import select
import subprocess
import sys
from uuid import uuid4

from agentbridge import Bridge
from fixtures.test_checkpoint_fixture import execution, prepared, terminal


def call(process, action, params, *, verify):
    request_id = str(uuid4())
    def write(document):
        process.stdin.write(json.dumps(document).encode() + b'\n')
        process.stdin.flush()
    write({'id': request_id, 'action': action, 'params': params})
    challenges = []
    for _ in range(5):
        assert select.select([process.stdout], [], [], 10)[0]
        value = json.loads(process.stdout.readline())
        assert value['id'] == request_id
        if 'verify' not in value:
            return value, challenges
        challenges.append(value)
        write({'id': request_id, 'nonce': value['nonce'],
               'verified': verify(value['verify'], value['scope'])})
    raise AssertionError('Unbounded verification challenges')


def test_host_pipe_challenges_exact_scope_and_keeps_failed_proof_held(tmp_path):
    bridge = prepared(tmp_path, durable=False)
    execution(bridge)
    bridge.store.finish('turn-1', 'completed')
    original = terminal(bridge).data
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    process = subprocess.Popen([sys.executable, '-m', 'agentbridge.checkpoint.host_upgrade',
        '--root', str(bridge.root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env)
    try:
        identity, _ = call(process, 'identity', {}, verify=lambda *_: False)
        assert identity['result']['instance_count'] == 1
        operation_id, proof_ref = str(uuid4()), str(uuid4())
        params = {'operation_id': operation_id, 'owner_ref': 'fixture-owner', 'proof_ref': proof_ref}
        refused, challenges = call(process, 'begin', params, verify=lambda *_: False)
        assert refused['error'] == 'checkpoint_busy' and len(challenges) == 1
        assert not Bridge(bridge.root).checkpoints.identity()['enabled']
        assert challenges[0]['scope']['store_id'] == identity['result']['store_id']
        accepted, _ = call(process, 'begin', params, verify=lambda kind, _: kind == 'quiescence')
        assert accepted['result']['operation_id'] == operation_id
        expected = {'native_id': bridge.get_session('instance')['native_id'],
                    'turn_id': 'turn-1', 'after_seq': terminal(bridge).seq}
        staged, challenges = call(process, 'stage', {'instance_id': 'instance',
            'operation_id': operation_id, 'expected': expected}, verify=lambda *_: True)
        assert staged['result']['checkpoint']['native_id'] == expected['native_id']
        assert len(challenges) == 2
        ref = str(uuid4())
        confirmed, challenges = call(process, 'confirm', {'instance_id': 'instance',
            'operation_id': operation_id, 'reconciliation_ref': ref},
            verify=lambda kind, scope: kind == 'reconciliation' and scope == {
                **staged['result'], 'reconciliation_ref': ref})
        assert confirmed['result'] == staged['result'] and len(challenges) == 1
        assert terminal(Bridge(bridge.root)).data == original
    finally:
        process.stdin.close()
        process.wait(timeout=5)
        process.stdout.close()
        assert not process.stderr.read()
        process.stderr.close()
