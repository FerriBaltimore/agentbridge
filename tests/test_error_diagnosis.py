import json

import pytest

from agentbridge import Account, Bridge, BridgeError
from agentbridge import error_diagnosis
from agentbridge.error_evidence import capture


def setup(tmp_path, monkeypatch):
    bridge = Bridge(tmp_path / 'store')
    bridge.register(Account('diagnostic', 'cursor', key_env='FIXTURE_CURSOR_KEY'))
    monkeypatch.setenv('FIXTURE_CURSOR_KEY', 'private-credential')
    case = bridge.error_learning.capture('claude', capture({'message': 'quota allocation exhausted'}))
    return bridge, case['id']


def test_diagnosis_idempotence_pending_review_and_restart(tmp_path, monkeypatch):
    bridge, case_id = setup(tmp_path, monkeypatch)
    calls = []
    def execute(payload, credentials, timeout):
        calls.append(payload)
        assert credentials == {'FIXTURE_CURSOR_KEY': 'private-credential'}
        assert timeout == 20
        return {'status': 'proposed', 'target_code': 'quota_exhausted'}
    monkeypatch.setattr(error_diagnosis, 'execute', execute)
    kwargs = {'account_ref': 'diagnostic', 'model': 'fixture', 'idempotency_key': 'diagnose-once', 'timeout': 20}
    first = bridge.error_diagnose(case_id, **kwargs)
    assert first['state'] == 'completed'
    restarted = Bridge(bridge.root)
    assert restarted.error_diagnose(case_id, **kwargs) == first and len(calls) == 1
    proposal = restarted.error_proposal(first['result']['proposal_id'])
    assert proposal['status'] == 'proposed'
    assert proposal['provenance'] == {'source': 'ai_session', 'operation_id': first['operation_id']}
    assert b'private-credential' not in bridge.store.path.read_bytes()
    with pytest.raises(BridgeError, match='another request'):
        restarted.error_diagnose(case_id, **{**kwargs, 'model': 'different'})


def test_unknown_diagnostic_outcome_does_not_relaunch(tmp_path, monkeypatch):
    bridge, case_id = setup(tmp_path, monkeypatch)
    calls = []
    def execute(*_):
        calls.append(1)
        raise KeyboardInterrupt()
    monkeypatch.setattr(error_diagnosis, 'execute', execute)
    kwargs = {'account_ref': 'diagnostic', 'model': 'fixture', 'idempotency_key': 'interrupted'}
    with pytest.raises(KeyboardInterrupt):
        bridge.error_diagnose(case_id, **kwargs)
    result = Bridge(bridge.root).error_diagnose(case_id, **kwargs)
    assert result['state'] == 'submitted' and result['retryable'] is False
    assert len(calls) == 1


def test_reconcile_proposal_saved_before_receipt(tmp_path, monkeypatch):
    bridge, case_id = setup(tmp_path, monkeypatch)
    def execute(*_):
        with bridge.store.connect() as db:
            operation_id = db.execute('SELECT operation_id FROM error_diagnoses').fetchone()[0]
        bridge.error_learning.propose(case_id, {'status': 'proposed', 'target_code': 'quota_exhausted'},
            provenance={'source': 'ai_session', 'operation_id': operation_id})
        raise KeyboardInterrupt()
    monkeypatch.setattr(error_diagnosis, 'execute', execute)
    with pytest.raises(KeyboardInterrupt):
        bridge.error_diagnose(case_id, account_ref='diagnostic', model='fixture', idempotency_key='saved')
    receipt = Bridge(bridge.root).error_diagnosis('saved')
    assert receipt['state'] == 'completed' and receipt['result']['proposal_id']


@pytest.mark.parametrize('value', [
    {'status': 'proposed', 'target_code': 'quota_exhausted', 'secret': 'private-body'},
    {'status': 'execute', 'target_code': 'run_shell'},
    {'status': 'proposed', 'target_code': 'unknown-private-code'},
])
def test_invalid_ai_result_is_not_persisted_or_recursively_diagnosed(tmp_path, monkeypatch, value):
    bridge, case_id = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(error_diagnosis, 'execute', lambda *_: value)
    result = bridge.error_diagnose(case_id, account_ref='diagnostic', model='fixture', idempotency_key='invalid')
    assert result['state'] == 'failed'
    assert len(bridge.error_cases()['items']) == 1
    assert b'private' not in bridge.store.path.read_bytes()


def test_insufficient_evidence_stays_unclassified(tmp_path, monkeypatch):
    bridge, case_id = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(error_diagnosis, 'execute', lambda *_: {'status': 'insufficient_evidence', 'target_code': None})
    result = bridge.error_diagnose(case_id, account_ref='diagnostic', model='fixture', idempotency_key='insufficient')
    proposal = bridge.error_proposal(result['result']['proposal_id'])
    assert proposal['status'] == 'insufficient_evidence'
    with pytest.raises(BridgeError):
        bridge.error_validate(proposal['id'])


@pytest.mark.parametrize('mode', ['completed', 'timeout', 'oversized'])
def test_supervisor_enforces_bounds_and_cleans_ephemeral_home(tmp_path, monkeypatch, mode):
    import os
    from pathlib import Path
    import subprocess
    import sys
    script = tmp_path / 'diagnostic.py'
    script.write_text('import json,os,sys,time\n'
        'sys.stdin.read()\n'
        "assert os.environ['HOME'] == os.getcwd()\n"
        "assert os.environ.get('UNRELATED_SECRET') is None\n"
        "assert os.environ['FIXTURE_KEY'] == 'credential-only-in-memory'\n"
        + ('time.sleep(10)\n' if mode == 'timeout' else
           "sys.stdout.write('x'*9000)\nsys.stdout.flush()\n" if mode == 'oversized' else
           "print(json.dumps({'status':'insufficient_evidence','target_code':None}))\n"))
    original = subprocess.Popen
    children, folders = [], []
    def popen(argv, **kwargs):
        folders.append(Path(kwargs['cwd']))
        child = original([sys.executable, str(script)], **kwargs)
        children.append(child)
        return child
    monkeypatch.setenv('UNRELATED_SECRET', 'must-not-leak')
    monkeypatch.setattr(subprocess, 'Popen', popen)
    if mode == 'completed':
        assert error_diagnosis.execute({}, {'FIXTURE_KEY': 'credential-only-in-memory'}, 2)['status'] == 'insufficient_evidence'
    else:
        with pytest.raises(BridgeError):
            error_diagnosis.execute({}, {'FIXTURE_KEY': 'credential-only-in-memory'}, .3)
    assert children[0].poll() is not None
    assert not folders[0].exists()
    with pytest.raises(ProcessLookupError):
        os.killpg(children[0].pid, 0)
