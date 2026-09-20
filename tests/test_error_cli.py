"""CLI error review exercises persistent services with isolated provider fixtures."""
import json

import pytest

from agentbridge import Account, Bridge
from agentbridge import error_diagnosis
from agentbridge.cli import main
from agentbridge.error_evidence import capture
from agentbridge.provider_errors import normalize


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


def test_explicit_diagnostic_selection_replays_and_never_activates(tmp_path, monkeypatch, capsys):
    bridge, case, _ = seed(tmp_path)
    bridge.register(Account('diagnostic-cursor', 'cursor', key_env='FIXTURE_DIAGNOSIS_KEY'))
    monkeypatch.setenv('FIXTURE_DIAGNOSIS_KEY', 'fixture-only-credential')
    calls = []
    def execute(payload, credentials, timeout):
        calls.append(payload)
        assert payload['model'] == 'fixture-model'
        assert credentials == {'FIXTURE_DIAGNOSIS_KEY': 'fixture-only-credential'}
        assert timeout == 25
        return {'status': 'proposed', 'target_code': 'provider_unavailable'}
    monkeypatch.setattr(error_diagnosis, 'execute', execute)
    arguments = ['diagnose', case['id'], '--account-ref', 'diagnostic-cursor', '--model', 'fixture-model',
                 '--idempotency-key', 'cli-diagnostic', '--timeout', '25']
    receipt = invoke(bridge.root, capsys, *arguments)
    assert receipt['state'] == 'completed'
    assert invoke(bridge.root, capsys, *arguments) == receipt and len(calls) == 1
    assert invoke(bridge.root, capsys, 'diagnosis', 'cli-diagnostic') == receipt
    proposal = invoke(bridge.root, capsys, 'proposal', receipt['result']['proposal_id'])
    assert proposal['status'] == 'proposed'
    with bridge.store.connect() as db:
        assert db.execute('SELECT count(*) FROM error_rules').fetchone()[0] == 0


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


def test_diagnosis_requires_account_model_and_key_before_provider_work(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(error_diagnosis, 'execute', lambda *_: calls.append('unexpected'))
    with pytest.raises(SystemExit) as failure:
        main(['--root', str(tmp_path / 'not-created'), 'errors', 'diagnose', 'case'])
    assert failure.value.code == 2 and calls == []
    assert not (tmp_path / 'not-created').exists()
    assert '--account-ref' in capsys.readouterr().err


def test_error_help_explains_commands_without_opening_a_store(tmp_path, capsys):
    root = tmp_path / 'not-created'
    with pytest.raises(SystemExit) as finished:
        main(['--root', str(root), 'errors'])
    assert finished.value.code == 0 and not root.exists()
    text = capsys.readouterr().out
    assert 'diagnose' in text and 'deactivate' in text and 'JSON' in text
