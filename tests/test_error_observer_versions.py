"""Bound version observations and attribute unknown errors to admitted releases."""
import sys

import pytest

from agentbridge import Account, Bridge, RunOptions
from agentbridge import error_observer as observer
from agentbridge.catalog_process import read_output
from agentbridge.provider_contracts import ContractRegistry
from agentbridge.provider_errors import normalize
from fixtures.test_proxy_account_fixture import register_verified_proxy_account


def admitted(tmp_path, turn_id='run'):
    bridge = Bridge(tmp_path / 'state')
    account = register_verified_proxy_account(bridge.store, 'fixture', 19432)
    bridge.store.add_session('fixture-session', account.id, str(tmp_path), 'fixture-model')
    session = bridge.get_session('fixture-session')
    bridge.store.admit(turn_id, session['id'], 'fixture', RunOptions(), None)
    return bridge, account


@pytest.mark.parametrize(('engine', 'output', 'expected'), [
    ('codex', 'codex-cli 0.153.0\n', '0.153.0'),
    ('claude', '2.1.266 (Claude Code)\n', '2.1.266'),
])
def test_native_version_query_is_bounded_and_does_not_receive_auth_env(
        tmp_path, monkeypatch, engine, output, expected):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture-never-used')
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-never-used')
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'private'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'private'))
    def query(command, *, env, timeout, max_bytes):
        assert command == [engine, '--version']
        assert timeout == 2 and max_bytes == 256
        assert not {'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'} & env.keys()
        return output.encode()
    monkeypatch.setattr(observer, 'read_output', query)
    assert observer.provider_version(Account('fixture', engine, home=tmp_path)) == expected


@pytest.mark.parametrize('script', [
    'import os;os.write(1,b"x"*4096)',
    'import time;time.sleep(30)',
    'import os;os.write(1,b"\\xff")',
    'raise SystemExit(1)',
])
def test_native_version_bad_output_timeout_and_exit_are_unknown(tmp_path, monkeypatch, script):
    def query(command, *, env, timeout, max_bytes):
        assert timeout == 2 and max_bytes == 256
        return read_output([sys.executable, '-c', script], env=env,
                           timeout=.1, max_bytes=max_bytes)
    monkeypatch.setattr(observer, 'read_output', query)
    assert observer.provider_version(Account('fixture', 'codex', home=tmp_path)) is None


def test_custom_launcher_is_not_probed(tmp_path, monkeypatch):
    monkeypatch.setattr(observer, 'read_output', lambda *a, **kw: pytest.fail('No native process expected'))
    assert observer.provider_version(Account('custom', 'codex', home=tmp_path, command=('fixture',))) is None


@pytest.mark.parametrize('version', ['0.153.0', None, 'future-private-value', False])
def test_error_observer_uses_saved_receipt_even_if_version_unknown(tmp_path, monkeypatch, version):
    bridge, account = admitted(tmp_path)
    ContractRegistry(bridge.store).record_run('run', {'engine': 'codex', 'version': version})
    monkeypatch.setattr(observer, 'provider_version', lambda account: pytest.fail('Admission receipt must win'))
    issue = normalize('codex', {'code': 'unknown-fixture-code'})
    result = observer.ErrorObserver(bridge.store, account, 'run')(issue)
    case = bridge.error_learning.get_case(result['details']['case_id'])
    assert case['provider_version'] == ('0.153.0' if version == '0.153.0' else None)


def test_rule_application_uses_admission_version_after_cli_upgrade(tmp_path, monkeypatch):
    bridge, account = admitted(tmp_path)
    ContractRegistry(bridge.store).record_run('run', {'engine': 'codex', 'version': '0.153.0'})
    issue = normalize('codex', {'code': 'unknown-fixture-code'})
    evidence = issue['details']['unknown_evidence']
    case = bridge.error_learning.capture('codex', evidence, provider_version='0.153.0')
    proposal = bridge.error_propose(case['id'], {'status': 'proposed', 'target_code': 'billing_required'})
    checked = bridge.error_validate(proposal['id'])
    bridge.error_activate(proposal['id'], checked['revision'])
    monkeypatch.setattr(observer, 'provider_version', lambda account: '0.154.0')
    result = observer.ErrorObserver(bridge.store, account, 'run')(issue)
    assert result['code'] == 'billing_required'
    assert result['details']['detection'] == 'learned_rule'


def test_legacy_error_observation_probes_only_once(tmp_path, monkeypatch):
    bridge, account = admitted(tmp_path, 'legacy-run')
    queries = []
    def query(account):
        queries.append(True)
        return '0.153.0'
    monkeypatch.setattr(observer, 'provider_version', query)
    observe = observer.ErrorObserver(bridge.store, account, 'legacy-run')
    observe(normalize('codex', {'code': 'unknown-fixture-code'}))
    observe(normalize('codex', {'code': 'another-fixture-code'}))
    assert queries == [True]
