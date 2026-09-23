"""Proxy-only diagnosis preserves historical receipts without executing AI."""
import json

import pytest

from agentbridge import Bridge, BridgeError
from agentbridge import error_diagnosis
from agentbridge.error_evidence import capture
from agentbridge.store import dumps


def test_diagnosis_rejects_before_account_lookup_or_receipt_creation(tmp_path):
    bridge = Bridge(tmp_path / 'store')
    with bridge.store.connect() as db:
        before = {row[0] for row in db.execute('SELECT name FROM sqlite_master WHERE type="table"')}
    with pytest.raises(BridgeError) as failure:
        bridge.error_diagnose('missing-case', account_ref='missing-account', model='fixture',
                              idempotency_key='never-submitted')
    assert failure.value.code == 'unsupported_operation'
    with bridge.store.connect() as db:
        after = {row[0] for row in db.execute('SELECT name FROM sqlite_master WHERE type="table"')}
    assert after == before
    assert 'error_diagnoses' not in after


def test_historical_completed_and_failed_receipts_remain_readable(tmp_path):
    bridge = Bridge(tmp_path / 'store')
    error_diagnosis._initialize(bridge.store)
    with bridge.store.connect() as db:
        db.execute('INSERT INTO error_diagnoses VALUES(?,?,?,?,?,?,?)',
                   ('saved-success', 'operation-success', '{}', 'completed',
                    dumps({'proposal_id': 'saved-proposal'}), 1.0, 2.0))
        db.execute('INSERT INTO error_diagnoses VALUES(?,?,?,?,?,?,?)',
                   ('saved-failure', 'operation-failure', '{}', 'failed',
                    dumps({'code': 'diagnosis_failed', 'retryable': False}), 3.0, 4.0))
    restarted = Bridge(bridge.root)
    completed = restarted.error_diagnosis('saved-success')
    failed = restarted.error_diagnosis('saved-failure')
    assert completed == {'operation_id': 'operation-success', 'state': 'completed',
                         'result': {'proposal_id': 'saved-proposal'}, 'created_at': 1.0,
                         'updated_at': 2.0, 'retryable': False, 'may_be_running': False}
    assert failed['state'] == 'failed'
    assert failed['result'] == {'code': 'diagnosis_failed', 'retryable': False}
    with pytest.raises(BridgeError) as missing:
        restarted.error_diagnosis('never-submitted')
    assert missing.value.code == 'not_found'


def test_historical_submitted_receipt_reconciles_saved_proposal(tmp_path):
    bridge = Bridge(tmp_path / 'store')
    operation_id = 'a' * 32
    case = bridge.error_learning.capture('codex', capture({'message': 'capacity unavailable'}))
    proposal = bridge.error_learning.propose(case['id'],
        {'status': 'proposed', 'target_code': 'provider_unavailable'},
        provenance={'source': 'ai_session', 'operation_id': operation_id})
    error_diagnosis._initialize(bridge.store)
    with bridge.store.connect() as db:
        db.execute('INSERT INTO error_diagnoses VALUES(?,?,?,?,?,?,?)',
                   ('saved-submitted', operation_id, '{}', 'submitted', None, 1.0, 1.0))
    receipt = Bridge(bridge.root).error_diagnosis('saved-submitted')
    assert receipt['state'] == 'completed'
    assert receipt['result'] == {'proposal_id': proposal['id']}
    assert receipt['retryable'] is False and receipt['may_be_running'] is False
    assert Bridge(bridge.root).error_diagnosis('saved-submitted') == receipt


def test_historical_submitted_receipt_without_proposal_stays_uncertain(tmp_path):
    bridge = Bridge(tmp_path / 'store')
    error_diagnosis._initialize(bridge.store)
    with bridge.store.connect() as db:
        db.execute('INSERT INTO error_diagnoses VALUES(?,?,?,?,?,?,?)',
                   ('saved-uncertain', 'b' * 32, '{}', 'submitted', None, 1.0, 1.0))
    receipt = Bridge(bridge.root).error_diagnosis('saved-uncertain')
    assert receipt['state'] == 'submitted' and receipt['result'] is None
    assert receipt['retryable'] is False and receipt['may_be_running'] is True


def test_historical_failure_adapter_exposes_only_known_fields():
    value = {'status': 'failed', 'reason': 'provider_failed', 'phase': 'send',
             'provider_code': 'provider_unavailable', 'private_body': 'secret'}
    result = error_diagnosis.safe_failure(value)
    assert result == {'code': 'diagnosis_failed', 'retryable': False,
                      'reason': 'provider_failed', 'phase': 'send',
                      'provider_code': 'provider_unavailable'}
    assert 'secret' not in json.dumps(result)
    assert error_diagnosis.safe_failure({'status': 'completed'}) is None
    assert error_diagnosis.safe_failure({'status': 'failed', 'reason': [], 'phase': {}}) == {
        'code': 'diagnosis_failed', 'retryable': False}
