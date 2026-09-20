"""Native pipe faults produce safe execution evidence without live providers."""
import io
import json
import os
import sqlite3
import sys

import pytest

from agentbridge.errors import BridgeError
from agentbridge import interactive_worker
from agentbridge.provider_channel import ProviderChannel
from agentbridge.protocols import Parser
from agentbridge.execution_outcome import finish


@pytest.mark.parametrize(('body', 'code'), [
    ('', 'provider_connection_lost'),
    ('{"type":"PRIVATE TRUNCATED BODY"', 'provider_protocol_error'),
    ('PRIVATE MALFORMED BODY\n', 'provider_protocol_error'),
    ('["PRIVATE ARRAY BODY"]\n', 'provider_protocol_error'),
])
def test_eof_and_corrupt_frames_are_unknown_execution(tmp_path, body, code):
    script = f'import sys; sys.stdout.write({body!r}); sys.stdout.flush()'
    with ProviderChannel([sys.executable, '-c', script], cwd=tmp_path, env=os.environ.copy()) as channel:
        with pytest.raises(BridgeError) as failure:
            channel.receive(2)
    assert failure.value.code == code
    assert failure.value.phase == 'execution'
    assert failure.value.outcome == 'unknown'
    assert failure.value.retryable is False
    assert 'PRIVATE' not in json.dumps(failure.value.safe_data())
    assert channel.process.poll() is not None


def test_native_receive_timeout_is_not_an_admission_failure(tmp_path):
    with ProviderChannel([sys.executable, '-c', 'import time; time.sleep(30)'],
                         cwd=tmp_path, env=os.environ.copy()) as channel:
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
    parsed = Parser('claude', lambda *_: None)
    parsed.feed({'type': 'bridge_error', 'error': {'code': code}, 'outcome': outcome})
    result, error, issue = finish(parsed, exit_code=1)
    assert (result, error) == (state, code)
    assert issue['outcome'] == outcome


def test_duplex_setup_storage_failure_emits_safe_machine_error(tmp_path, monkeypatch, capsys):
    payload = {'root': str(tmp_path), 'secret_names': []}
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
def test_permission_persistence_failure_closes_native_channel_and_reports_unknown(
        tmp_path, monkeypatch, capsys, operation):
    delivered = []
    closed = []
    payload = {'root': str(tmp_path), 'secret_names': [], 'command': ['fixture'],
               'cwd': str(tmp_path), 'engine': 'claude', 'turn_id': 'fixture-turn',
               'options': {'permission_mode': 'default', 'timeout': 1}}
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(payload)))
    class Channel:
        def __init__(self, *_, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *_): closed.append(True)
    class Broker:
        def __init__(self, *_): pass
        def request(self, *_, **kwargs):
            if operation == 'request':
                raise sqlite3.OperationalError('PRIVATE REQUEST BODY')
            return 'fixture-permission'
        def wait(self, *_): return 'allow'
        def delivered(self, *_):
            raise sqlite3.OperationalError('PRIVATE DELIVERY BODY')
    def execute(channel, value, emit, approve):
        approve({'operation': 'fixture'}, lambda value: delivered.append(value))
    monkeypatch.setattr(interactive_worker, 'ProviderChannel', Channel)
    monkeypatch.setattr(interactive_worker, 'Permissions', Broker)
    monkeypatch.setattr(interactive_worker, 'execute_claude', execute)
    with pytest.raises(SystemExit):
        interactive_worker.main()
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert value['error']['code'] == 'worker_failed'
    assert value['outcome'] == 'unknown'
    assert delivered == ([] if operation == 'request' else ['allow'])
    assert closed == [True]
    assert 'PRIVATE' not in output.out + output.err
