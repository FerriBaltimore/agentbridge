"""Bounded subprocess transport for native version inspection uses local fixtures."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import tracemalloc

import pytest

from agentbridge import catalog_process
from agentbridge import provider_catalog
from agentbridge.errors import BridgeError


def command(body):
    return [sys.executable, '-c', body]


def test_partial_utf8_output_and_stdin_eof_are_preserved():
    value = json.dumps([{'id': 'future', 'display_name': 'Català 日本語'}], ensure_ascii=False).encode()
    script = ('import os,sys\nassert sys.stdin.read() == ""\n'
              f'for part in {list(value)!r}: os.write(1, bytes([part]))\n')
    assert catalog_process.read_output(command(script), env={}) == value


def test_worker_can_use_an_explicit_inspection_directory(tmp_path):
    output = catalog_process.read_output(command('import os;print(os.getcwd())'), env={}, cwd=tmp_path)
    assert Path(output.decode().strip()) == tmp_path


def test_exact_output_limit_is_accepted_and_one_more_byte_is_rejected():
    limit = catalog_process.MAX_OUTPUT_BYTES
    data = catalog_process.read_output(command(f'import os;os.write(1,b" "*({limit}-2)+b"[]")'), env={})
    assert len(data) == limit and json.loads(data) == []
    with pytest.raises(BridgeError) as error:
        catalog_process.read_output(command(f'import os;os.write(1,b" "*{limit}+b"x")'), env={})
    assert error.value.code == 'provider_protocol_error'


def test_flood_is_rejected_without_capturing_all_output():
    tracemalloc.start()
    try:
        with pytest.raises(BridgeError) as error:
            catalog_process.read_output(command('import os\nfor _ in range(48): os.write(1,b"x"*(512*1024))'), env={})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert error.value.code == 'provider_protocol_error'
    assert peak < 3 * catalog_process.MAX_OUTPUT_BYTES


@pytest.mark.parametrize('body', ['import time;time.sleep(20)',
    'import os,time;os.write(1,b"[");time.sleep(20)', 'import os,time;os.close(1);time.sleep(20)'])
def test_deadline_covers_silence_partial_output_and_waiting_after_eof(body):
    before = time.monotonic()
    with pytest.raises(BridgeError) as error:
        catalog_process.read_output(command(body), env={}, timeout=.15)
    assert error.value.code == 'provider_timeout'
    assert time.monotonic() - before < 2


def test_nonzero_exit_and_launch_failure_are_visible_without_raw_output(tmp_path):
    with pytest.raises(BridgeError) as error:
        catalog_process.read_output(command('import sys;print("PRIVATE_BODY");sys.exit(1)'), env={})
    assert error.value.code == 'provider_unavailable' and 'PRIVATE_BODY' not in str(error.value)
    with pytest.raises(BridgeError) as error:
        catalog_process.read_output([str(tmp_path / 'does-not-exist')], env={})
    assert error.value.code == 'provider_unavailable'


def running(pid):
    """An orphan zombie can await init reaping but cannot perform more work."""
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


@pytest.mark.parametrize('mode', ['inherited_pipe', 'closed_pipe', 'flood'])
def test_descendants_are_stopped_after_leader_exit_or_output_failure(tmp_path, monkeypatch, mode):
    pid_file = tmp_path / 'descendant.pid'
    inherited = mode == 'inherited_pipe'
    body = ('import os,subprocess,sys\nfrom pathlib import Path\n'
            f'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(20)"],'
            f'stdout={"None" if inherited else "subprocess.DEVNULL"})\n'
            f'Path({str(pid_file)!r}).write_text(str(child.pid))\n'
            + ('for _ in range(48): os.write(1,b"x"*(512*1024))\n' if mode == 'flood' else
               'print("[]",flush=True)\n'))
    original, leaders = subprocess.Popen, []
    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        leaders.append(process)
        return process
    monkeypatch.setattr(catalog_process.subprocess, 'Popen', launch)
    try:
        if mode == 'closed_pipe':
            assert catalog_process.read_output(command(body), env={}, timeout=1) == b'[]\n'
        else:
            with pytest.raises(BridgeError) as error:
                catalog_process.read_output(command(body), env={}, timeout=.4)
            assert error.value.code == ('provider_timeout' if inherited else 'provider_protocol_error')
        assert leaders[0].returncode is not None
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 1
        while running(pid) and time.monotonic() < deadline:
            time.sleep(.01)
        assert not running(pid)
    finally:
        for process in leaders:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)


def test_retired_provider_catalog_entry_point_does_not_launch_a_worker(monkeypatch):
    monkeypatch.setattr(catalog_process.subprocess, 'Popen',
                        lambda *_args, **_kwargs: pytest.fail('native catalog worker launched'))
    with pytest.raises(SystemExit) as error:
        provider_catalog.main()
    assert 'unavailable' in str(error.value)
