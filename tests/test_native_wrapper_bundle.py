"""The native wrapper boots from a host bundle before Codex's environment is reduced."""

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

import pytest

from agentbridge.native_sandbox import available
from test_native_sandbox import _bwrap_args, _directory


ROOT = Path(__file__).resolve().parents[1]
FILES = ('interactive_worker.py', 'native_sandbox.py', 'execution_context.py')
CHECK_NATIVE = '''
import errno, importlib.util, os
from pathlib import Path
assert 'PYTHONPATH' not in os.environ
assert importlib.util.find_spec('psycopg') is None
for forbidden in ('/state/canary', '/opt/agentbridge/python/RUNTIME.json'):
    try:
        Path(forbidden).read_bytes()
    except OSError as error:
        assert error.errno == errno.EACCES
    else:
        raise AssertionError('Native process reached private worker input')
'''
CHECK_MCP = '''
        import subprocess
        mcp = start['params']['config']['mcp_servers']['agentbridge_execution']
        assert mcp['env'] == {'PYTHONPATH': '/opt/agentbridge/src'}
        assert 'PYTHONPATH' not in mcp['env_vars']
        env = {'PATH': '/usr/bin:/bin', **mcp['env'],
               **{key: os.environ[key] for key in mcp['env_vars']}}
        initialized = subprocess.run([mcp['command'], *mcp['args']], env=env,
            input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
                  + '\\n', capture_output=True, text=True, timeout=5)
        assert initialized.returncode == 0, initialized.stderr
        assert json.loads(initialized.stdout)['result']['serverInfo']['name'] == (
            'agentbridge-execution')
'''


def candidate(tmp_path, baseline):
    """Only three current source files replace the immutable LAB baseline, via new inodes."""
    bundle = tmp_path / 'bundle'
    shutil.copytree(baseline, bundle, copy_function=os.link)
    manifest_path = bundle / 'SNAPSHOT-MANIFEST.json'
    manifest = json.loads(manifest_path.read_bytes())
    for name in FILES:
        relative = 'src/agentbridge/' + name
        body = (ROOT / relative).read_bytes()
        target = bundle / relative
        temporary = target.with_suffix('.candidate')
        temporary.write_bytes(body)
        temporary.chmod(0o644)
        temporary.replace(target)
        manifest[relative] = sha256(body).hexdigest()
    temporary = bundle / 'manifest.candidate'
    temporary.write_text(json.dumps(manifest))
    temporary.chmod(0o644)
    temporary.replace(manifest_path)
    return bundle


@pytest.mark.skipif(not available() or not Path('/usr/bin/bwrap').exists(),
                    reason='The production bwrap and Landlock profile is unavailable')
def test_interactive_bundle_boots_wrapper_then_hides_dependencies_from_native(tmp_path, monkeypatch):
    selected = os.environ.get('AGENTBRIDGE_TEST_NATIVE_WRAPPER_BUNDLE')
    if not selected:
        pytest.skip('Set the explicit verified host-bundle LAB root')
    baseline = Path(selected)
    assert (baseline / 'SNAPSHOT-MANIFEST.json').is_file()
    bundle = candidate(tmp_path, baseline)
    monkeypatch.setattr('test_native_sandbox.ROOT', bundle)
    tmp_path.chmod(0o755)
    for path in ('state/codex-runtime/evaluation', 'home', 'tmp/private', 'cwd'):
        _directory(tmp_path / path)
    (tmp_path / 'state/canary').write_text('synthetic private worker data')
    (tmp_path / 'state/canary').chmod(0o644)
    args = _bwrap_args(tmp_path)
    clean = subprocess.run(args + ['--', '/usr/bin/python3', '-I', '-c',
        'import importlib.util; assert importlib.util.find_spec("agentbridge") is None'],
        env={}, capture_output=True, text=True, timeout=10)
    assert clean.returncode == 0, clean.stderr
    source = (ROOT / 'tests/fixtures/test_duplex_provider.py').read_text()
    source = CHECK_NATIVE + source.replace(
        "        policy = start['params']['approvalPolicy']", CHECK_MCP
        + "\n        policy = start['params']['approvalPolicy']")
    operation = str(uuid4())
    environment = {'AGENTBRIDGE_MCP_SOCKET_PATH': '/tmp/private/mcp.sock',
                   'AGENTBRIDGE_MCP_OPERATION_ID': operation,
                   'AGENTBRIDGE_MCP_CAPABILITY': 'synthetic-test-capability-only'}
    for key, value in environment.items():
        args += ['--setenv', key, value]
    payload = {'engine': 'codex', 'root': '/state', 'turn_id': 'wrapper-probe',
               'secret_names': [], 'mcp_enabled': True, 'cwd': '/workspace/isolated',
               'command': ['/usr/bin/python3', '-c', source], 'prompt': 'native-wrapper-ok',
               'options': {'permission_mode': 'dontAsk', 'sandbox': 'read-only', 'timeout': 5}}
    result = subprocess.run(args + ['--chdir', '/workspace/isolated', '--',
        '/usr/bin/python3', '-P', '-m', 'agentbridge.interactive_worker'],
        input=json.dumps(payload), env={}, capture_output=True, text=True, timeout=20)
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert result.returncode == 0, (events, result.stderr)
    assert any(event['type'] == 'thread.started' for event in events)
    assert any(event['type'] == 'turn.completed' for event in events)
    assert not any(event['type'] == 'bridge_error' for event in events)
