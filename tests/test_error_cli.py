"""CLI error review exercises persistent services with isolated provider fixtures."""
import json

import pytest

from agentbridge import Bridge
from agentbridge import error_diagnosis
from agentbridge.cli import main
from agentbridge.error_evidence import capture
from agentbridge.provider_errors import normalize
from agentbridge.store import dumps


def invoke(root, capsys, *args):
    main(['--root', str(root), 'errors', *args])
    captured = capsys.readouterr()
    assert captured.err == ''
    return json.loads(captured.out)


def seed(tmp_path):
    bridge = Bridge(tmp_path / 'bridge')
    evidence = capture({'code': 'new-provider-failure', 'message': 'Capacity unavailable'})
    case = bridge.error_learning.capture('claude', evidence, provider_version='2.1.266')
    return bridge, case, evidence


def test_case_queries_page_persistent_safe_evidence(tmp_path, capsys):
    bridge, case, _ = seed(tmp_path)
    second = bridge.error_learning.capture('codex', capture({'message': 'Another unfamiliar event'}))
    page = invoke(bridge.root, capsys, 'cases', '--limit', '1')
    assert len(page['items']) == 1 and page['items'][0]['id'] == case['id']
    assert page['next_cursor'] == 1
    page = invoke(bridge.root, capsys, 'cases', '--limit', '1', '--cursor', '1')
    assert page['items'][0]['id'] == second['id'] and page['next_cursor'] is None
    assert invoke(bridge.root, capsys, 'case', case['id']) == case


def test_diagnosis_reports_proxy_unavailable_without_creating_operation(tmp_path, capsys):
    bridge, case, _ = seed(tmp_path)
    with pytest.raises(SystemExit) as failure:
        main(['--root', str(bridge.root), 'errors', 'diagnose', case['id']])
    assert failure.value.code == 1
    output = capsys.readouterr()
    assert output.out == ''
    assert json.loads(output.err)['error'] == 'unsupported_operation'
    with bridge.store.connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='error_diagnoses'").fetchone() is None


def test_diagnosis_command_reads_historical_receipt(tmp_path, capsys):
    bridge, _, _ = seed(tmp_path)
    error_diagnosis._initialize(bridge.store)
    with bridge.store.connect() as db:
        db.execute('INSERT INTO error_diagnoses VALUES(?,?,?,?,?,?,?)',
                   ('saved', 'operation-saved', '{}', 'failed',
                    dumps({'code': 'diagnosis_failed', 'retryable': False}), 1.0, 1.0))
    receipt = invoke(bridge.root, capsys, 'diagnosis', 'saved')
    assert receipt['state'] == 'failed'
    assert receipt['result'] == {'code': 'diagnosis_failed', 'retryable': False}


def test_review_activation_and_rollback_are_separate_revision_checked_commands(tmp_path, capsys):
    bridge, case, evidence = seed(tmp_path)
    item = bridge.error_propose(case['id'], {'status': 'proposed', 'target_code': 'provider_unavailable'})
    validated = invoke(bridge.root, capsys, 'validate', item['id'])
    assert validated['validation']['semantic_verification'] is False
    original = normalize('claude', {}, outcome='unknown')
    assert bridge.error_learning.apply('claude', evidence, original, '2.1.266') == original
    with pytest.raises(SystemExit) as failure:
        main(['--root', str(bridge.root), 'errors', 'activate', item['id'], '--expected-revision', '1'])
    assert failure.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == '' and json.loads(captured.err)['error'] == 'version_conflict'
    rule = invoke(bridge.root, capsys, 'activate', item['id'], '--expected-revision', str(validated['revision']))
    assert rule['state'] == 'active'
    assert invoke(bridge.root, capsys, 'rule', rule['id']) == rule
    classified = bridge.error_learning.apply('claude', evidence, original, '2.1.266')
    assert classified['code'] == 'provider_unavailable' and classified['retryable'] is False
    inactive = invoke(bridge.root, capsys, 'deactivate', rule['id'], '--expected-revision', str(rule['revision']))
    assert inactive['state'] == 'inactive' and inactive['revision'] == rule['revision'] + 1
    assert bridge.error_learning.apply('claude', evidence, original, '2.1.266') == original


def test_command_failures_are_json_on_stderr_without_partial_success(tmp_path, capsys):
    with pytest.raises(SystemExit) as failure:
        main(['--root', str(tmp_path), 'errors', 'case', 'missing-case'])
    assert failure.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert json.loads(captured.err)['error'] == 'error_record_not_found'


def test_diagnosis_requires_case_before_opening_store(tmp_path, capsys):
    with pytest.raises(SystemExit) as failure:
        main(['--root', str(tmp_path / 'not-created'), 'errors', 'diagnose'])
    assert failure.value.code == 2
    assert not (tmp_path / 'not-created').exists()
    assert 'CASE' in capsys.readouterr().err


def test_error_help_explains_commands_without_opening_a_store(tmp_path, capsys):
    root = tmp_path / 'not-created'
    with pytest.raises(SystemExit) as finished:
        main(['--root', str(root), 'errors'])
    assert finished.value.code == 0 and not root.exists()
    text = capsys.readouterr().out
    assert 'diagnose' in text and 'deactivate' in text and 'JSON' in text
