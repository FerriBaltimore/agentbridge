"""The native inputs-only process cannot inspect the worker's shared mounts."""

import errno
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import time

import pytest

from agentbridge.native_sandbox import available
from agentbridge.execution_context import codex_config


ROOT = Path(__file__).resolve().parents[1]


def _directory(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o777)


def _bwrap_args(root):
    args = ['/usr/bin/bwrap', '--unshare-all', '--unshare-user', '--uid', '10000',
            '--gid', '10000', '--disable-userns', '--assert-userns-disabled',
            '--die-with-parent', '--new-session', '--clearenv', '--cap-drop', 'ALL']
    for path in ('/usr', '/lib', '/lib64'):
        if Path(path).exists():
            args += ['--ro-bind', path, path]
    args += ['--symlink', 'usr/bin', '/bin', '--proc', '/proc', '--dev', '/dev',
             '--tmpfs', '/run', '--ro-bind', str(ROOT), '/opt/agentbridge']
    for name, target in (('state', '/state'), ('home', '/home/worker'), ('tmp', '/tmp')):
        args += ['--bind', str(root / name), target]
    args += ['--ro-bind', str(root / 'cwd'), '/workspace/isolated']
    for key, value in {'PATH': '/usr/bin:/bin', 'PYTHONPATH': '/opt/agentbridge/src',
                       'CODEX_HOME': '/state/codex-runtime/evaluation',
                       'TMPDIR': '/tmp/private',
                       'HOME': '/state/codex-runtime/evaluation'}.items():
        args += ['--setenv', key, value]
    return args


@pytest.mark.skipif(not Path('/usr/bin/bwrap').exists() or not available()
                    or not Path('/usr/bin/python3').exists(),
                    reason='The production Linux bwrap and Landlock profile is unavailable')
def test_inputs_only_native_process_cannot_read_shared_mount_canaries(tmp_path):
    tmp_path.chmod(0o755)
    for path in ('state/codex-runtime/evaluation', 'home', 'tmp/private', 'cwd'):
        _directory(tmp_path / path)
    for path in ('state/canary', 'home/canary', 'tmp/canary'):
        (tmp_path / path).write_text('another tenant secret')
        (tmp_path / path).chmod(0o644)
    code = '''
import json
from pathlib import Path
paths = ['/state/canary', '/home/worker/canary', '/tmp/canary',
         '/proc/self/environ']
result = {}
for path in paths:
    try:
        result[path] = Path(path).read_text()
    except OSError as error:
        result[path] = error.errno
for path in ('/state/codex-runtime/evaluation/own', '/tmp/private/own'):
    Path(path).write_text('private')
    result[path] = Path(path).read_text()
print(json.dumps(result))
'''
    args = _bwrap_args(tmp_path) + ['--chdir', '/workspace/isolated', '--',
            '/usr/bin/python3', '-P', '-m', 'agentbridge.native_sandbox',
            '/usr/bin/python3', '-c', code]
    result = subprocess.run(args, capture_output=True, text=True, timeout=15,
                            check=False, env={'PATH': os.environ.get('PATH', '/usr/bin')})
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    for path in ('/state/canary', '/home/worker/canary', '/tmp/canary',
                 '/proc/self/environ'):
        assert observed[path] == errno.EACCES
    assert observed['/state/codex-runtime/evaluation/own'] == 'private'
    assert observed['/tmp/private/own'] == 'private'


def test_native_sandbox_requires_landlock_and_private_paths(monkeypatch, tmp_path):
    import agentbridge.native_sandbox as sandbox

    home, temporary, cwd = (tmp_path / name for name in ('home', 'tmp', 'cwd'))
    for path in (home, temporary, cwd):
        path.mkdir()
    executable = shutil.which('python3')
    monkeypatch.setattr(sandbox, 'available', lambda: False)
    with pytest.raises(OSError, match='Landlock'):
        sandbox.restrict(cwd=cwd, home=home, temporary=temporary,
                         executable=str(Path(executable).resolve()))


@pytest.mark.skipif(not available(), reason='Landlock unavailable')
def test_inputs_only_rejects_an_ambient_workspace(tmp_path):
    from agentbridge.native_sandbox import restrict

    home = tmp_path / 'state/codex-runtime/instance'
    temporary, cwd = (tmp_path / name for name in ('tmp', 'cwd'))
    for path in (home, temporary, cwd):
        path.mkdir(parents=True)
    (cwd / 'another-input').write_text('not selected')
    with pytest.raises(ValueError, match='must be empty'):
        restrict(cwd=cwd, home=home, temporary=temporary,
                 executable=str(Path(shutil.which('python3')).resolve()))


@pytest.mark.skipif(not available(), reason='Landlock unavailable')
def test_native_sandbox_rejects_a_workspace_inside_private_state(tmp_path):
    from agentbridge.native_sandbox import restrict

    home = tmp_path / 'state/codex-runtime/instance'
    temporary = tmp_path / 'native-tmp'
    private_workspace = tmp_path / 'state/managed-proxies'
    for path in (home, temporary, private_workspace):
        path.mkdir(parents=True)
    with pytest.raises(ValueError, match='private worker state'):
        restrict(cwd=private_workspace, home=home, temporary=temporary,
                 executable=str(Path('/usr/bin/python3').resolve()), inputs_only=False)


@pytest.mark.skipif(not available(), reason='Landlock unavailable')
def test_native_launcher_rejects_codex_executable_inside_workspace(tmp_path):
    workspace = tmp_path / 'workspace'
    home = tmp_path / 'state/codex-runtime/instance'
    temporary = tmp_path / 'native-tmp'
    for path in (workspace, home, temporary):
        path.mkdir(parents=True)
    fake = workspace / 'codex'
    fake.write_text('#!/bin/sh\necho workspace-executable-ran\n')
    fake.chmod(0o700)
    environment = {**os.environ, 'PATH': f'{workspace}:/usr/bin',
                   'CODEX_HOME': str(home), 'HOME': str(home),
                   'TMPDIR': str(temporary), 'PYTHONPATH': str(ROOT / 'src')}
    result = subprocess.run(
        ['/usr/bin/python3', '-P', '-m', 'agentbridge.native_sandbox',
         '--normal', '--', 'codex'], cwd=workspace, env=environment,
        capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert 'workspace-executable-ran' not in result.stdout


@pytest.mark.parametrize('inputs_only', (True, False))
@pytest.mark.skipif(not available() or not Path('/usr/bin/bwrap').exists(),
                    reason='The production Linux isolation profile is unavailable')
def test_real_codex_starts_in_isolation_without_credentials(tmp_path, inputs_only):
    """Optional offline acceptance: set a reviewed standalone Codex executable path."""
    selected = os.environ.get('AGENTBRIDGE_CODEX_ACCEPTANCE_BIN')
    if not selected:
        pytest.skip('Set AGENTBRIDGE_CODEX_ACCEPTANCE_BIN for offline native acceptance')
    binary = Path(selected).resolve(strict=True)
    assert binary.is_file() and os.access(binary, os.X_OK)
    tmp_path.chmod(0o755)
    for path in ('state/codex-runtime/evaluation', 'home', 'tmp/private', 'cwd'):
        _directory(tmp_path / path)
    if not inputs_only:
        (tmp_path / 'cwd' / 'selected.txt').write_text('selected input')
    mode = ['--inputs-only' if inputs_only else '--normal', '--']
    args = _bwrap_args(tmp_path) + [
        '--ro-bind', str(binary), '/opt/provider/codex',
        '--chdir', '/workspace/isolated', '--', '/usr/bin/python3', '-P', '-m',
        'agentbridge.native_sandbox', *mode, '/opt/provider/codex', 'app-server', '--stdio',
    ]
    requests = [
        {'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'agentbridge-isolation-probe', 'version': '1'}}},
        {'method': 'initialized', 'params': {}},
        {'id': 2, 'method': 'skills/list', 'params': {
            'cwds': ['/workspace/isolated'], 'forceReload': True}},
        {'id': 3, 'method': 'thread/start', 'params': {
            'cwd': '/workspace/isolated', 'approvalPolicy': 'never',
            'sandbox': 'read-only', 'ephemeral': True,
            'config': codex_config(selected_context=True,
                execution_mode='evaluation_inputs_only' if inputs_only else 'normal')}},
    ]
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env={})
    try:
        process.stdin.write(b''.join((json.dumps(item) + '\n').encode() for item in requests))
        process.stdin.flush()
        responses, buffer, deadline = {}, b'', time.monotonic() + 10
        while time.monotonic() < deadline and len(responses) < 3 and process.poll() is None:
            if not select.select([process.stdout], [], [], .1)[0]:
                continue
            buffer += os.read(process.stdout.fileno(), 65536)
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                value = json.loads(line)
                if value.get('id') in (1, 2, 3):
                    responses[value['id']] = value
        assert set(responses) == {1, 2, 3}
        assert all(isinstance(value.get('result'), dict) and 'error' not in value
                   for value in responses.values())
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
