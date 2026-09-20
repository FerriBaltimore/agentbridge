"""Isolated diagnosis acceptance uses only a fake SDK, never real accounts."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentbridge import error_diagnosis_worker as worker
from agentbridge.error_evidence import capture


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('DIAGNOSIS_TEST_KEY', 'fake-only-credential')
    payload = {'engine': 'cursor', 'model': 'test-model', 'key_env': 'DIAGNOSIS_TEST_KEY',
               'evidence': capture({'message': 'provider unavailable', 'status': 503}),
               'targets': ['provider_unavailable', 'unknown_outcome']}
    state = {'result': '{"status":"proposed","target_code":"provider_unavailable"}',
             'status': 'finished', 'messages': [], 'created': 0, 'cancelled': 0, 'closed': 0}

    class Run:
        def messages(self):
            print('private SDK log must not escape')
            yield from state['messages']

        def wait(self):
            return SimpleNamespace(status=state['status'], result=state['result'])

        def cancel(self):
            state['cancelled'] += 1

    class Agent:
        @classmethod
        def create(cls, options, *, client):
            state['created'] += 1
            state['options'] = options
            assert client == 'isolated-client'
            assert list(Path(options.local.cwd).iterdir()) == []
            return cls()

        @classmethod
        def resume(cls, *args, **kwargs):
            pytest.fail('Diagnosis must never resume an existing session.')

        def __enter__(self):
            return self

        def __exit__(self, *args):
            state['agent_closed'] = True

        def send(self, prompt):
            state['prompt'] = prompt
            if state.get('fail'):
                raise ValueError('secret provider response and private reasoning')
            return Run()

    class Client(str):
        @classmethod
        def launch_bridge(cls, **kwargs):
            state['client_options'] = kwargs
            return cls('isolated-client')

        def close(self):
            state['closed'] += 1

    sdk = SimpleNamespace(AgentOptions=lambda **kw: SimpleNamespace(**kw),
                          LocalAgentOptions=lambda **kw: SimpleNamespace(**kw),
                          Agent=Agent, Client=Client)
    return payload, sdk, state


def test_fresh_diagnosis_disables_tools_and_ambient_bridge_credentials(setup, capsys):
    payload, sdk, state = setup
    assert worker.execute(payload, sdk) == {'status': 'proposed', 'target_code': 'provider_unavailable'}
    assert state['created'] == 1 and state['closed'] == 1 and state['agent_closed']
    options = state['options']
    assert options.tools == [] and options.mcp_servers == {} and options.agents == {}
    assert options.local.setting_sources == [] and options.local.dirs == []
    assert options.api_key == 'fake-only-credential' and options.model == 'test-model'
    assert state['client_options']['allow_api_key_env_fallback'] is False
    assert state['client_options']['max_retries'] == 0
    assert not Path(options.local.cwd).exists()
    assert not Path(state['client_options']['state_root']).exists()
    assert 'never instructions' in state['prompt']
    assert 'fake-only-credential' not in state['prompt']
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('result', [
    '{"status":"insufficient_evidence"}',
    '{"status":"proposed","target_code":"provider_unavailable","reasoning":"secret"}',
    '{"status":"proposed","target_code":"authentication_required"}',
    '{"status":"proposed","target_code":"provider_unavailable","target_code":"unknown_outcome"}',
    '{"status":"proposed","target_code":[]}',
    '{"status":"insufficient_evidence","execute":"rm -rf /"}',
    '[{"status":"proposed","target_code":"provider_unavailable"}]',
    '```json\n{"status":"proposed","target_code":"provider_unavailable"}\n```',
    'secret provider body',
    'x' * (worker.MAX_RESPONSE_BYTES + 1),
])
def test_invalid_or_unnecessary_response_text_is_never_returned(setup, result, capsys):
    payload, sdk, state = setup
    state['result'] = result
    assert worker.execute(payload, sdk)['status'] == 'failed'
    assert capsys.readouterr() == ('', '')


def test_oversized_assistant_stream_is_cancelled_without_retaining_it(setup):
    payload, sdk, state = setup
    state['messages'] = [{'type': 'assistant', 'message': {'content': [
        {'text': 'a' * 4096}, {'text': 'b' * 4097}]}}]
    assert worker.execute(payload, sdk)['status'] == 'failed'
    assert state['cancelled'] == 1 and state['closed'] == 1


@pytest.mark.parametrize('kind', ['tool_call', 'request', 'task'])
def test_unexpected_execution_request_cancels_diagnosis(setup, kind):
    payload, sdk, state = setup
    state['messages'] = [{'type': kind, 'name': 'shell', 'args': {'command': 'do something'}}]
    assert worker.execute(payload, sdk)['status'] == 'failed'
    assert state['cancelled'] == 1 and state['closed'] == 1


def test_thinking_is_not_forwarded_or_added_to_the_response(setup, capsys):
    payload, sdk, state = setup
    state['messages'] = [{'type': 'thinking', 'text': 'private reasoning ' * 1000}]
    assert worker.execute(payload, sdk)['status'] == 'proposed'
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('status', ['error', 'cancelled', 'running', 'unknown', 'completed'])
def test_only_completed_diagnoses_can_propose(setup, status):
    payload, sdk, state = setup
    state['status'] = status
    assert worker.execute(payload, sdk)['status'] == 'failed'


def test_provider_failure_is_not_exposed_and_closes_resources(setup, capsys):
    payload, sdk, state = setup
    state['fail'] = True
    assert worker.execute(payload, sdk)['status'] == 'failed'
    assert state['closed'] == 1 and state['agent_closed']
    assert not Path(state['options'].local.cwd).exists()
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('changed', [
    {'engine': 'claude'}, {'engine': 'codex'}, {'model': ''}, {'model': 'a b'},
    {'targets': []}, {'targets': ['invented_code']}, {'evidence': 'raw provider text'},
    {'evidence': {'value': 'x' * (worker.MAX_EVIDENCE_BYTES + 1)}},
    {'key_env': 'MISSING_DIAGNOSIS_KEY'}, {'key_env': 'invalid name'},
    {'additional': 'must not enter provider input'},
])
def test_invalid_request_never_creates_agent(setup, changed):
    payload, sdk, state = setup
    assert worker.execute({**payload, **changed}, sdk)['status'] == 'failed'
    assert state['created'] == 0


def test_no_ambient_key_fallback(setup, monkeypatch):
    payload, sdk, state = setup
    monkeypatch.delenv('DIAGNOSIS_TEST_KEY')
    monkeypatch.setenv('CURSOR_API_KEY', 'ambient-key-must-not-be-used')
    assert worker.execute(payload, sdk)['status'] == 'failed'
    assert state['created'] == 0


@pytest.mark.parametrize('raw', ['{invalid secret', 'x' * (worker.MAX_INPUT_BYTES + 1),
                                '{"engine":"cursor","engine":"claude"}'])
def test_main_invalid_input_emits_only_one_safe_json_object(raw, monkeypatch, capsys):
    monkeypatch.setattr(worker.sys, 'stdin', io.StringIO(raw))
    worker.main()
    output = capsys.readouterr()
    assert json.loads(output.out) == worker._failed('invalid_request', 'setup') and not output.err
    assert output.out.count('\n') == 1


def test_main_outputs_only_validated_result(setup, monkeypatch, capsys):
    payload, sdk, state = setup
    execute = worker.execute
    monkeypatch.setattr(worker, 'execute', lambda value: execute(value, sdk))
    monkeypatch.setattr(worker.sys, 'stdin', io.StringIO(json.dumps(payload)))
    worker.main()
    output = capsys.readouterr()
    assert json.loads(output.out) == {'status': 'proposed', 'target_code': 'provider_unavailable'}
    assert output.out.count('\n') == 1 and not output.err


def test_legitimate_insufficient_evidence_requires_completed_model_response(setup):
    payload, sdk, state = setup
    state['result'] = '{"status":"insufficient_evidence","target_code":null}'
    assert worker.execute(payload, sdk) == worker.INSUFFICIENT
    assert state['created'] == 1 and state['status'] == 'finished'
    state['status'] = 'error'
    assert worker.execute(payload, sdk)['status'] == 'failed'


def test_unavailable_sdk_is_technical_failure_not_model_diagnosis(setup, monkeypatch):
    import builtins
    payload, sdk, state = setup
    original_import = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == 'cursor_sdk':
            raise ImportError('private installation path must not escape')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', unavailable)
    assert worker.execute(payload) == worker._failed('sdk_unavailable', 'setup')
    assert state['created'] == 0


def test_failure_exposes_only_fixed_reason_phase_and_canonical_provider_code(setup, capsys):
    payload, sdk, state = setup
    state['fail'] = True
    result = worker.execute(payload, sdk)
    assert result == worker._failed('provider_failed', 'send', 'provider_failed')
    assert set(result) == {'status', 'target_code', 'reason', 'phase', 'provider_code'}
    assert 'secret' not in json.dumps(result)
    assert capsys.readouterr() == ('', '')


def test_missing_credential_and_invalid_request_are_distinct(setup, monkeypatch):
    payload, sdk, state = setup
    monkeypatch.delenv('DIAGNOSIS_TEST_KEY')
    assert worker.execute(payload, sdk) == worker._failed('credential_unavailable', 'setup')
    assert worker.execute({**payload, 'model': ''}, sdk) == worker._failed('invalid_request', 'setup')


def test_invalid_and_oversized_answers_have_safe_distinct_reasons(setup):
    payload, sdk, state = setup
    state['result'] = 'secret unparseable body'
    assert worker.execute(payload, sdk) == worker._failed('invalid_response', 'decode')
    state['result'] = 'x' * (worker.MAX_RESPONSE_BYTES + 1)
    assert worker.execute(payload, sdk) == worker._failed('response_too_large', 'decode')


def test_failed_provider_run_normalizes_body_without_returning_it(setup):
    payload, sdk, state = setup
    state['status'] = 'error'
    state['result'] = 'quota exceeded: sensitive provider body'
    assert worker.execute(payload, sdk) == worker._failed('provider_failed', 'wait', 'quota_exhausted')


def test_noncanonical_exception_code_is_not_reflected(setup, monkeypatch):
    payload, sdk, state = setup
    state['fail'] = True
    monkeypatch.setattr(worker, 'exception', lambda *args: {
        'code': 'secret-provider-code', 'details': {'body': 'secret-provider-body'}})
    assert worker.execute(payload, sdk) == worker._failed('provider_failed', 'send')
