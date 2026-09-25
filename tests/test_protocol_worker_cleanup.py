"""A completed provider wrapper cannot leave its owned descendants running."""
import json
import time

import pytest

from agentbridge import RunOptions
from agentbridge.protocols import Parser
from agentbridge.execution_outcome import finish
from agentbridge.process import alive
from agentbridge.session_events import routing
from agentbridge.worker import parse
from fixtures.test_proxy_worker_fixture import MODEL, bridge_with_proxy, management_server
from fixtures.test_process_descendants import kill_owned, owned_descendant


@pytest.mark.parametrize('inherit_pipe', [False, True])
def test_wrapper_exit_closes_descendants_instead_of_waiting_for_run_timeout(tmp_path, monkeypatch, inherit_pipe):
    pid_file = tmp_path / 'descendant.pid'
    release_file = tmp_path / 'release-provider'
    script = tmp_path / 'provider.py'
    script.write_text('''import json, signal, subprocess, sys, time
from pathlib import Path
child_code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
kwargs = {} if sys.argv[2] == 'inherit' else {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
child = subprocess.Popen([sys.executable, '-c', child_code], stdin=subprocess.DEVNULL, **kwargs)
Path(sys.argv[1]).write_text(str(child.pid))
print(json.dumps({'type':'thread.started','thread_id':'fixture-cleanup-history'}), flush=True)
while not Path(sys.argv[3]).exists():
    time.sleep(.02)
print(json.dumps({'type':'turn.completed'}), flush=True)
''')
    with management_server() as port:
        bridge = bridge_with_proxy(tmp_path, monkeypatch, port,
            command=('/usr/bin/python3', str(script), str(pid_file),
                     'inherit' if inherit_pipe else 'closed', str(release_file)))
        session = bridge.session('fixture', tmp_path, model=MODEL)
        run = bridge.submit(session['id'], 'fixture',
                            options=RunOptions(timeout=20, sandbox='workspace-write'))
        owned = None
        try:
            deadline = time.monotonic() + 5
            while (not pid_file.exists() or not pid_file.read_text()) and time.monotonic() < deadline:
                time.sleep(.02)
            assert pid_file.exists() and pid_file.read_text()
            owned = owned_descendant(run, int(pid_file.read_text()))
            release_file.touch()
            result = run.wait(8)
            assert result['state'] == 'completed'
            deadline = time.monotonic() + 2
            while alive(*owned) and time.monotonic() < deadline:
                time.sleep(.02)
            assert not alive(*owned)
        finally:
            if run.status not in ('completed', 'failed', 'cancelled', 'interrupted', 'incomplete'):
                run.stop(wait=True)
            if owned:
                kill_owned(*owned)


def test_corrupt_stdout_followed_by_complete_does_not_hide_missing_evidence():
    events = []
    parsed = Parser('codex', lambda kind, data: events.append((kind, data)))
    parse(b'PRIVATE INVALID PROVIDER BODY', parsed)
    parse(b'{"type":"turn.completed"}', parsed)
    assert finish(parsed)[:2] == ('interrupted', 'provider_protocol_error')
    assert 'PRIVATE INVALID PROVIDER BODY' not in json.dumps(events)


def test_older_claude_default_scope_is_distinct_from_unknown_future_scope():
    assert routing('claude', {})['scope'] == 'session'
    assert routing('claude', {'scope': 'future_scope'})['scope'] == 'unknown'
    assert routing('claude', {'scope': None})['scope'] == 'unknown'
