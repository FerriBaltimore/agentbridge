"""Native pipe faults produce safe execution evidence without live providers."""
import io
import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

from agentbridge.errors import BridgeError
from agentbridge import interactive_worker
from agentbridge.provider_channel import ProviderChannel
from agentbridge.protocols import Parser
from agentbridge.execution_outcome import finish


def native_environment(tmp_path):
    workspace = tmp_path / 'workspace'
    home = tmp_path / 'state/codex-runtime/instance'
    temporary = tmp_path / 'native-tmp'
    for path in (workspace, home, temporary):
        path.mkdir(parents=True)
    environment = {**os.environ, 'CODEX_HOME': str(home), 'HOME': str(home),
                   'TMPDIR': str(temporary),
                   'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    return workspace, environment


@pytest.mark.parametrize(('body', 'code'), [
    ('', 'provider_connection_lost'),
    ('{"type":"PRIVATE TRUNCATED BODY"', 'provider_protocol_error'),
    ('PRIVATE MALFORMED BODY\n', 'provider_protocol_error'),
    ('["PRIVATE ARRAY BODY"]\n', 'provider_protocol_error'),
])
def test_eof_and_corrupt_frames_are_unknown_execution(tmp_path, body, code):
    script = f'import sys; sys.stdout.write({body!r}); sys.stdout.flush()'
    workspace, environment = native_environment(tmp_path)
    with ProviderChannel(['/usr/bin/python3', '-c', script],
                         cwd=workspace, env=environment) as channel:
        with pytest.raises(BridgeError) as failure:
            channel.receive(2)
    assert failure.value.code == code
    assert failure.value.phase == 'execution'
    assert failure.value.outcome == 'unknown'
    assert failure.value.retryable is False
    assert 'PRIVATE' not in json.dumps(failure.value.safe_data())
    assert channel.process.poll() is not None


def test_native_receive_timeout_is_not_an_admission_failure(tmp_path):
    workspace, environment = native_environment(tmp_path)
    with ProviderChannel(['/usr/bin/python3', '-c', 'import time; time.sleep(30)'],
                         cwd=workspace, env=environment) as channel:
        with pytest.raises(BridgeError) as failure:
            channel.receive(.02)
    assert failure.value.code == 'provider_timeout'
    assert failure.value.phase == 'execution'
    assert failure.value.outcome == 'unknown'
    assert failure.value.retryable is False
    assert channel.process.poll() is not None


@pytest.mark.parametrize(('code', 'outcome', 'state'), [
    ('worker_failed', 'unknown', 'interrupted'),
    ('provider_protocol_error', 'not_started', 'failed'),
    ('provider_connection_lost', 'unknown', 'interrupted'),
])
def test_wrapper_failures_preserve_known_admission_vs_unknown_execution(code, outcome, state):
    parsed = Parser('codex', lambda *_: None)
    parsed.feed({'type': 'bridge_error', 'error': {'code': code}, 'outcome': outcome})
    result, error, issue = finish(parsed, exit_code=1)
    assert (result, error) == (state, code)
    assert issue['outcome'] == outcome


def test_duplex_setup_storage_failure_emits_safe_machine_error(tmp_path, monkeypatch, capsys):
    payload = {'root': str(tmp_path), 'engine': 'codex', 'secret_names': []}
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))
    def fail(*_):
        raise sqlite3.OperationalError('PRIVATE STORAGE BODY')
    monkeypatch.setattr(interactive_worker, 'Permissions', fail)
    with pytest.raises(SystemExit) as stopped:
        interactive_worker.main()
    assert stopped.value.code == 1
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert value['type'] == 'bridge_error'
    assert value['error']['code'] == 'worker_failed'
    assert 'PRIVATE STORAGE BODY' not in output.out + output.err


@pytest.mark.parametrize('operation', ['request', 'delivered'])
def test_permission_persistence_failure_closes_codex_channel_and_reports_unknown(
        tmp_path, monkeypatch, capsys, operation):
    delivered = []
    closed = []
    payload = {'root': str(tmp_path), 'secret_names': [], 'command': ['fixture'],
               'cwd': str(tmp_path), 'engine': 'codex', 'turn_id': 'fixture-turn',
               'options': {'permission_mode': 'default', 'timeout': 1,
                           'sandbox': 'read-only'}}
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'state/codex-runtime/instance'))
    class Channel:
        def __init__(self, *_, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_): closed.append(True)
    class Broker:
        def __init__(self, store): self.store = store
        def request(self, *_, **kwargs):
            if operation == 'request':
                raise sqlite3.OperationalError('PRIVATE REQUEST BODY')
            return 'fixture-permission'
        def wait(self, *_): return 'allow'
        def delivered(self, *_):
            raise sqlite3.OperationalError('PRIVATE DELIVERY BODY')
    class Control:
        def __init__(self, channel, value, emit, approve, steering=None):
            self.approve = approve
        def execute(self):
            self.approve({'operation': 'fixture'}, lambda value: delivered.append(value))
    monkeypatch.setattr(interactive_worker, 'ProviderChannel', Channel)
    monkeypatch.setattr(interactive_worker, 'Permissions', Broker)
    monkeypatch.setattr(interactive_worker, 'CodexControl', Control)
    with pytest.raises(SystemExit):
        interactive_worker.main()
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert value['error']['code'] == 'worker_failed'
    assert value['outcome'] == 'unknown'
    assert delivered == ([] if operation == 'request' else ['allow'])
    assert closed == [True]
    assert 'PRIVATE' not in output.out + output.err
