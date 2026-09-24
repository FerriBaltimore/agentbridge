"""Local faults cannot silently leave a claimed execution running forever."""
import asyncio
import io
import json
import sqlite3
import sys
import time

import pytest

from agentbridge import RunOptions
from agentbridge import worker
from agentbridge.errors import BridgeError
from agentbridge.process import identity
from fixtures.test_proxy_worker_fixture import (
    CLIENT_KEY_ENV, MANAGEMENT_KEY_ENV, MODEL, bridge_with_proxy, management_server,
)


@pytest.fixture
def local_proxy():
    with management_server() as port:
        yield port


def admitted(tmp_path, monkeypatch, local_proxy,
             script='import json; print(json.dumps({"type":"turn.completed"}))'):
    native = tmp_path / 'provider.py'
    native.write_text(script)
    bridge = bridge_with_proxy(tmp_path, monkeypatch, local_proxy,
                               command=('/usr/bin/python3', str(native)))
    session = bridge.session('fixture', tmp_path, model=MODEL)
    run_id, _ = bridge.store.admit('fixture-run', session['id'], 'fixture', RunOptions(timeout=2), None)
    return bridge, bridge.run(run_id)


def in_process(monkeypatch, bridge, run):
    monkeypatch.setattr(worker, 'Store', lambda _: bridge.store)
    monkeypatch.setattr(sys, 'argv', ['worker', str(bridge.root), run.id])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({
        CLIENT_KEY_ENV: 'fixture-client-key', MANAGEMENT_KEY_ENV: 'fixture-management-key'})))
    monkeypatch.setattr(worker.signal, 'signal', lambda *_: None)
    monkeypatch.setattr(worker, 'identity', lambda _: 'fixture-dead-owner')


def test_claimed_setup_failure_is_visible_without_waiting_for_recovery(tmp_path, monkeypatch, local_proxy):
    bridge, run = admitted(tmp_path, monkeypatch, local_proxy)
    in_process(monkeypatch, bridge, run)
    original = bridge.store.get
    def get(table, key):
        if table == 'sessions':
            raise sqlite3.OperationalError('PRIVATE DATABASE BODY')
        return original(table, key)
    monkeypatch.setattr(bridge.store, 'get', get)
    with pytest.raises(SystemExit) as stopped:
        worker.main()
    assert stopped.value.code == 1
    assert (run.status, run.snapshot['error']) == ('failed', 'worker_failed')
    data = [event.data for event in run.events()]
    assert data[-1]['error_detail']['outcome'] == 'not_started'
    assert 'PRIVATE DATABASE BODY' not in json.dumps(data)


def test_provider_spawn_failure_has_not_started_outcome(tmp_path, monkeypatch, local_proxy):
    bridge, run = admitted(tmp_path, monkeypatch, local_proxy)
    in_process(monkeypatch, bridge, run)
    def popen(*_, **kwargs):
        raise FileNotFoundError('PRIVATE EXECUTABLE PATH')
    monkeypatch.setattr(worker.subprocess, 'Popen', popen)
    with pytest.raises(SystemExit):
        worker.main()
    assert (run.status, run.snapshot['error']) == ('failed', 'provider_unavailable')
    terminal = list(run.events())[-1].data
    assert terminal['error_detail']['phase'] == 'launch'
    assert terminal['error_detail']['outcome'] == 'not_started'
    assert 'PRIVATE EXECUTABLE PATH' not in json.dumps(terminal)


@pytest.mark.parametrize('code', ['provider_contract_unverified', 'provider_contract_changed'])
def test_contract_recheck_failure_preserves_safe_details_before_native_launch(tmp_path, monkeypatch, local_proxy, code):
    bridge, run = admitted(tmp_path, monkeypatch, local_proxy)
    in_process(monkeypatch, bridge, run)
    def verify(*_):
        raise BridgeError(code, 'A reviewed contract is required.', phase='launch',
                          details={'compatibility': {'status': 'unindexed_version'}})
    monkeypatch.setattr(worker.ContractRegistry, 'verify_run', verify)
    with pytest.raises(SystemExit):
        worker.main()
    assert (run.status, run.snapshot['error']) == ('failed', code)
    issue = list(run.events())[-1].data['error_detail']
    assert issue['outcome'] == 'not_started' and issue['phase'] == 'launch'
    assert issue['details']['compatibility']['status'] == 'unindexed_version'


@pytest.mark.parametrize('failed_kind', ['run_started', 'tool_result'])
def test_repeated_emit_failure_does_not_skip_terminal_commit(tmp_path, monkeypatch, local_proxy, failed_kind):
    bridge, run = admitted(tmp_path, monkeypatch, local_proxy, '''import json
print(json.dumps({'type':'item.started','item':{'id':'tool','type':'command_execution','command':'fixture'}}))
print(json.dumps({'type':'turn.completed'}))
''')
    in_process(monkeypatch, bridge, run)
    original = bridge.store.emit
    def emit(run_id, kind, data):
        if kind in {failed_kind, 'error'}:
            raise sqlite3.OperationalError('PRIVATE DISK BODY')
        return original(run_id, kind, data)
    monkeypatch.setattr(bridge.store, 'emit', emit)
    with pytest.raises(SystemExit):
        worker.main()
    assert run.status == 'interrupted'
    assert run.snapshot['error'] == 'worker_failed'
    assert identity(run.snapshot['child_pid']) is None
    assert list(run.events())[-1].kind == 'run_finished'
    assert 'PRIVATE DISK BODY' not in json.dumps([event.data for event in run.events()])


def test_terminal_commit_failure_is_retried_as_unknown_not_success(tmp_path, monkeypatch, local_proxy):
    bridge, run = admitted(tmp_path, monkeypatch, local_proxy)
    in_process(monkeypatch, bridge, run)
    original = bridge.store.finish
    calls = []
    def finish(run_id, state, *args):
        calls.append(state)
        if len(calls) == 1:
            raise sqlite3.OperationalError('PRIVATE DISK BODY')
        return original(run_id, state, *args)
    monkeypatch.setattr(bridge.store, 'finish', finish)
    with pytest.raises(SystemExit):
        worker.main()
    assert calls == ['completed', 'interrupted']
    assert run.status == 'interrupted'
    assert list(run.events())[-1].data['outcome'] == 'unknown'


def test_permanent_storage_failure_exits_and_later_recovers(tmp_path, monkeypatch, local_proxy, capsys):
    bridge, run = admitted(tmp_path, monkeypatch, local_proxy)
    in_process(monkeypatch, bridge, run)
    def fail(*_, **kwargs):
        raise sqlite3.OperationalError('PRIVATE DATABASE BODY')
    with monkeypatch.context() as outage:
        outage.setattr(bridge.store, 'emit', fail)
        outage.setattr(bridge.store, 'finish', fail)
        with pytest.raises(SystemExit) as stopped:
            worker.main()
        assert stopped.value.code == 1
    assert 'PRIVATE DATABASE BODY' not in capsys.readouterr().err
    assert bridge.recover(turn_id=run.id)['interrupted'] == [run.id]
    assert run.snapshot['error'] == 'worker_lost'


def test_partial_prompt_delivery_cannot_report_success(tmp_path, monkeypatch, local_proxy):
    bridge, admitted_run = admitted(tmp_path, monkeypatch, local_proxy)
    # Close the unused fixture admission to make a public large-input run.
    bridge.store.finish(admitted_run.id, 'cancelled', 'user_stop')
    run = bridge.submit(admitted_run.snapshot['session_id'], 'fixture ' * 100000,
                        options=RunOptions(timeout=2))
    assert run.wait(5)['state'] == 'interrupted'
    assert run.snapshot['error'] == 'provider_connection_lost'
    assert list(run.events())[-1].data['outcome'] == 'unknown'


@pytest.mark.parametrize('asynchronous', [False, True])
def test_event_follow_reconciles_old_admission_without_worker(tmp_path, monkeypatch, local_proxy, asynchronous):
    bridge, run = admitted(tmp_path, monkeypatch, local_proxy)
    with bridge.store.connect() as db:
        db.execute('UPDATE runs SET created=? WHERE id=?', (time.time() - 20, run.id))
    if asynchronous:
        async def collect():
            return [event async for event in run.aevents(timeout=.5)]
        events = asyncio.run(collect())
    else:
        events = list(run.events(follow=True, timeout=.5))
    assert events[-1].kind == 'run_finished'
    assert run.status == 'interrupted'
    assert run.snapshot['error'] == 'worker_lost'
