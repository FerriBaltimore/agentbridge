"""Faults at the private worker-input boundary never restart a proxy turn."""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from agentbridge import Bridge, RunOptions, run_launcher
from agentbridge.errors import BridgeError
from agentbridge.process import identity
from fixtures.test_proxy_account_fixture import register_verified_proxy_account


def admitted(tmp_path):
    bridge = Bridge(tmp_path / 'store')
    register_verified_proxy_account(bridge.store, 'fixture', 8317)
    session_id, _ = bridge.store.add_session('fixture-session', 'fixture', str(tmp_path),
                                             'fixture-model')
    run_id, _ = bridge.store.admit('fixture-run', session_id, 'test', RunOptions(), 'same-request')
    return bridge, run_id


def wait_for(path):
    deadline = time.monotonic() + 3
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert path.exists()


def test_success_returns_worker_and_secrets_only_travel_over_stdin(tmp_path, monkeypatch):
    bridge, run_id = admitted(tmp_path)
    original = subprocess.Popen
    def launch(argv, **kwargs):
        assert argv[-2:] == [str(bridge.store.root), run_id]
        assert kwargs['start_new_session'] is True
        assert kwargs['env']['PYTHONPATH'].endswith('/src')
        assert 'FIXTURE_PRIVATE_VALUE' not in json.dumps([argv, kwargs['env']])
        return original([sys.executable, '-c',
            'import json,sys;assert json.load(sys.stdin)=={"FIXTURE_KEY":"FIXTURE_PRIVATE_VALUE"}'], **kwargs)
    monkeypatch.setattr(run_launcher.subprocess, 'Popen', launch)
    process = run_launcher.launch(bridge.store, run_id, {'FIXTURE_KEY': 'FIXTURE_PRIVATE_VALUE'})
    assert process.wait(timeout=3) == 0
    assert process.stdin.closed


def test_spawn_failure_is_persisted_as_known_not_started(tmp_path, monkeypatch):
    bridge, run_id = admitted(tmp_path)
    def fail(*args, **kwargs):
        raise OSError('PRIVATE EXCEPTION BODY')
    monkeypatch.setattr(run_launcher.subprocess, 'Popen', fail)
    with pytest.raises(BridgeError) as error:
        run_launcher.launch(bridge.store, run_id, {'FIXTURE_KEY': 'FIXTURE_PRIVATE_VALUE'})
    assert error.value.code == 'launch_failed' and error.value.outcome == 'not_started'
    assert error.value.phase == 'launch' and error.value.details['state_persisted'] is True
    row = bridge.store.get('runs', run_id)
    assert (row['state'], row['error']) == ('failed', 'launch_failed')
    assert 'PRIVATE' not in json.dumps([str(error.value), error.value.safe_data(), row])


class BrokenInput:
    def __init__(self, stream, *, close_failure=False, marker=None):
        self.stream, self.close_failure, self.marker = stream, close_failure, marker
        self.raw = stream.raw

    def write(self, value):
        if not self.close_failure:
            raise BrokenPipeError('PRIVATE WRITE BODY')
        return self.stream.write(value)

    def close(self):
        self.stream.close()
        if self.marker:
            wait_for(self.marker)
        raise OSError('PRIVATE CLOSE BODY')


@pytest.mark.parametrize('close_failure', [False, True])
def test_delivery_failure_reaps_worker_and_preserves_unknown_outcome(tmp_path, monkeypatch, close_failure):
    bridge, run_id = admitted(tmp_path)
    original, workers = subprocess.Popen, []
    def launch(argv, **kwargs):
        process = original([sys.executable, '-c', 'import sys,time;sys.stdin.read();time.sleep(30)'], **kwargs)
        process.stdin = BrokenInput(process.stdin, close_failure=close_failure)
        workers.append(process)
        return process
    monkeypatch.setattr(run_launcher.subprocess, 'Popen', launch)
    with pytest.raises(BridgeError) as error:
        run_launcher.launch(bridge.store, run_id, {'FIXTURE_KEY': 'FIXTURE_PRIVATE_VALUE'})
    assert error.value.code == 'unknown_outcome' and error.value.outcome == 'unknown'
    assert error.value.retryable is False and error.value.details['cleanup_complete'] is True
    assert workers[0].poll() is not None
    row = bridge.store.get('runs', run_id)
    assert (row['state'], row['error']) == ('interrupted', 'unknown_outcome')
    assert list(bridge.run(run_id).events())[-1].data['outcome'] == 'unknown'
    assert 'PRIVATE' not in json.dumps([str(error.value), error.value.safe_data(), row])


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'cancelled'])
def test_late_pipe_failure_never_overwrites_a_saved_terminal_result(tmp_path, monkeypatch, terminal):
    bridge, run_id = admitted(tmp_path)
    original = subprocess.Popen
    def launch(argv, **kwargs):
        process = original([sys.executable, '-c', 'import time;time.sleep(30)'], **kwargs)
        process.stdin = BrokenInput(process.stdin)
        bridge.store.finish(run_id, terminal, 'provider_failed' if terminal == 'failed' else None)
        return process
    monkeypatch.setattr(run_launcher.subprocess, 'Popen', launch)
    with pytest.raises(BridgeError) as error:
        run_launcher.launch(bridge.store, run_id, {})
    assert error.value.outcome == 'unknown'
    assert bridge.store.get('runs', run_id)['state'] == terminal
    assert len([event for event in bridge.run(run_id).events() if event.kind == 'run_finished']) == 1


@pytest.mark.parametrize('ignore_term', [False, True])
def test_unrecorded_native_process_is_cleaned_after_worker_input_close_failure(tmp_path, monkeypatch, ignore_term):
    bridge, run_id = admitted(tmp_path)
    marker = tmp_path / 'native.pid'
    body = ('import signal,subprocess,sys,time\nfrom pathlib import Path\n'
            + ('signal.signal(signal.SIGTERM,signal.SIG_IGN)\n' if ignore_term else '') +
            'sys.stdin.read()\n'
            'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],start_new_session=True)\n'
            f'Path({str(marker)!r}).write_text(str(child.pid))\n'
            'time.sleep(30)\n')
    original, workers = subprocess.Popen, []
    def launch(argv, **kwargs):
        process = original([sys.executable, '-c', body], **kwargs)
        process.stdin = BrokenInput(process.stdin, close_failure=True, marker=marker)
        workers.append(process)
        return process
    monkeypatch.setattr(run_launcher.subprocess, 'Popen', launch)
    monkeypatch.setattr(run_launcher, 'CLEANUP_GRACE_SECONDS', .1)
    try:
        with pytest.raises(BridgeError) as error:
            run_launcher.launch(bridge.store, run_id, {})
        assert error.value.details['cleanup_complete'] is True
        assert workers[0].poll() is not None
        assert identity(int(marker.read_text())) is None
        assert bridge.store.get('runs', run_id)['child_pid'] is None
    finally:
        if marker.exists():
            try:
                os.killpg(int(marker.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_storage_failure_does_not_prevent_worker_cleanup_or_leak_error_body(tmp_path, monkeypatch):
    bridge, run_id = admitted(tmp_path)
    original, workers = subprocess.Popen, []
    def launch(argv, **kwargs):
        process = original([sys.executable, '-c', 'import time;time.sleep(30)'], **kwargs)
        process.stdin = BrokenInput(process.stdin)
        workers.append(process)
        return process
    def fail(*args, **kwargs):
        raise OSError('PRIVATE STORAGE BODY')
    monkeypatch.setattr(run_launcher.subprocess, 'Popen', launch)
    monkeypatch.setattr(bridge.store, 'get', fail)
    monkeypatch.setattr(bridge.store, 'finish', fail)
    with pytest.raises(BridgeError) as error:
        run_launcher.launch(bridge.store, run_id, {})
    assert workers[0].poll() is not None
    assert error.value.details == {'turn_id': run_id, 'state_persisted': False, 'cleanup_complete': True}
    assert 'PRIVATE' not in str(error.value)
