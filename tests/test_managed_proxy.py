"""Managed sidecar lifecycle with a local fixture executable and no real account."""

import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest

from agentbridge import Bridge
from agentbridge.proxy.managed import ManagedProxyClient, _socket_address
from agentbridge.proxy.management import ManagementClient
from agentbridge.proxy.route import ProxyRoute
from agentbridge.native_sandbox import wrap
from agentbridge.proxy.supervisor import (
    Supervisor, _account_dir, _pid_start, _write_record)


@pytest.fixture
def managed(tmp_path, monkeypatch):
    if not sys.platform.startswith("linux") or not hasattr(os, "memfd_create"):
        pytest.skip("Managed proxy requires Linux memfd support.")
    binary = tmp_path / "fixture-cliproxy"
    binary.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import json
        import sys

        with open(sys.argv[sys.argv.index('-config') + 1]) as stream:
            config = json.load(stream)
        key = config['remote-management']['secret-key']
        inventory = {name: [] for name in (
            'gemini-api-key', 'interactions-api-key', 'claude-api-key',
            'codex-api-key', 'xai-api-key', 'meta-api-key', 'vertex-api-key',
            'openai-compatibility')}
        inventory['plugins'] = {'enabled': False}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get('Authorization') != 'Bearer ' + key:
                    self.send_error(401)
                    return
                if self.path == '/v0/management/config':
                    value = inventory
                elif self.path == '/v0/management/auth-files':
                    value = {'files': []}
                else:
                    self.send_error(404)
                    return
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        HTTPServer(('127.0.0.1', config['port']), Handler).serve_forever()
    '''))
    binary.chmod(0o700)
    monkeypatch.setenv("AGENTBRIDGE_CLIPROXY_BIN", str(binary))
    state_root = tmp_path / ("long-state-directory-" + "x" * 90)
    with Bridge(state_root):
        client = ManagedProxyClient(state_root)
        try:
            yield client, state_root
        finally:
            try:
                client.shutdown()
            except Exception:
                pass


def test_managed_proxy_provisions_reconnects_and_retires_without_persisting_keys(managed):
    client, root = managed
    route = client.provision("fixture-account")
    assert client.is_managed(route, "fixture-account")
    assert not client.is_managed(route, "another-account")
    assert route == client.ensure("fixture-account", route["proxy_base_url"])
    proxy = ProxyRoute("fixture-account", route["proxy_base_url"], route["key_env"])
    ManagementClient(proxy, route["management_key_env"]).ensure_empty()

    account_dir = root / "managed-proxies" / "accounts" / "fixture-account"
    (account_dir / "auth" / "fixture-only.json").write_text("synthetic credential marker")
    client_key = os.environ[route["key_env"]].encode()
    management_key = os.environ[route["management_key_env"]].encode()
    for path in (account_dir / "route.json", root / "bridge.sqlite3"):
        content = path.read_bytes()
        assert client_key not in content and management_key not in content
    assert client.retire("fixture-account") == {
        "retired": True, "upstream_credential_removed": False}
    assert (account_dir / "auth" / "fixture-only.json").exists()


def test_managed_proxy_reattaches_after_supervisor_restart(managed):
    client, root = managed
    route = client.provision("fixture-reattach")
    record_path = root / "managed-proxies" / "accounts" / "fixture-reattach" / "route.json"
    first = json.loads(record_path.read_text())
    client_key = os.environ[route["key_env"]]
    management_key = os.environ[route["management_key_env"]]
    with _socket_address(client.directory) as address:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(address)
            supervisor_pid, _, _ = struct.unpack(
                "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    os.kill(supervisor_pid, signal.SIGKILL)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(supervisor_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    assert client.ensure("fixture-reattach", route["proxy_base_url"]) == route
    second = json.loads(record_path.read_text())
    assert second["pid"] == first["pid"]
    assert os.environ[route["key_env"]] == client_key
    assert os.environ[route["management_key_env"]] == management_key


@pytest.mark.parametrize('selected_context', [False, True], ids=['projection', 'selected-context'])
def test_native_provider_cannot_read_proxy_credential_or_authorize_supervisor(managed, selected_context):
    """A normal same-UID Codex child has no authority over the local proxy."""
    with tempfile.TemporaryDirectory(prefix='agentbridge-security-') as temporary:
        root = Path(temporary)
        state = root / 'state'
        client = ManagedProxyClient(state)
        try:
            route = client.provision('fixture-account')
            with _socket_address(client.directory) as address:
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect(address)
                    supervisor_pid, _, _ = struct.unpack('3i', connection.getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    connection.sendall(b'{"action":"auth_info"}\n')
                    auth_fd = json.loads(connection.makefile('rb').readline())['result']['auth_fd']
            assert Path(f'/proc/{supervisor_pid}/fd/{auth_fd}').exists()
            auth = state / 'managed-proxies/accounts/fixture-account/auth'
            (auth / 'canary.json').write_text('synthetic credential marker')
            home = state / 'codex-runtime' / 'instance'
            native_temp = root / 'native-tmp'
            workspace = root / 'workspace'
            for path in (home, native_temp, workspace):
                path.mkdir(parents=True)
            (workspace / 'selected.txt').write_text('selected input')
            script = textwrap.dedent('''\
                import ctypes
                import json
                import os
                from pathlib import Path
                import socket
                import struct

                root = Path(os.environ['TEST_ROOT'])
                endpoint = str(root / 'state/managed-proxies/supervisor.sock')
                pid = int(os.environ['TEST_SUPERVISOR_PID'])
                descriptor = int(os.environ['TEST_AUTH_FD'])
                try:
                    Path(f'/proc/{pid}/fd/{descriptor}').read_bytes()
                    proc_denied = False
                except OSError:
                    proc_denied = True
                socket_blocked = False
                supervisor_rejected = False
                try:
                    with socket.socket(socket.AF_UNIX) as connection:
                        connection.connect(endpoint)
                        actual_pid, _, _ = struct.unpack('3i', connection.getsockopt(
                            socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                        assert actual_pid == pid
                        request = {'action': 'ensure', 'account_id': 'fixture-account',
                                   'base_url': os.environ['TEST_BASE_URL'], 'auth': 'A' * 64}
                        connection.sendall((json.dumps(request) + '\\n').encode())
                        rejected = json.loads(connection.makefile('rb').readline())
                        supervisor_rejected = rejected.get('error', {}).get('code') == 'managed_proxy_auth_required'
                except (FileNotFoundError, PermissionError):
                    socket_blocked = True
                try:
                    (root / 'state/managed-proxies/accounts/fixture-account/auth/canary.json').read_text()
                    auth_denied = False
                except OSError:
                    auth_denied = True
                libc = ctypes.CDLL(None, use_errno=True)
                ptrace_denied = libc.ptrace(0, 0, 0, 0) == -1 and ctypes.get_errno() == 1
                print(json.dumps({
                    'auth_denied': auth_denied, 'proc_denied': proc_denied,
                    'supervisor_rejected': supervisor_rejected,
                    'socket_blocked': socket_blocked,
                    'ptrace_denied': ptrace_denied,
                    'workspace_readable': (root / 'workspace/selected.txt').read_text()
                        == 'selected input',
                    'management_env_absent': not any(name.startswith('AGENTBRIDGE_PROXY_')
                                                     for name in os.environ),
                }))
            ''')
            env = {name: value for name, value in os.environ.items()
                   if not name.startswith('AGENTBRIDGE_PROXY_')}
            env.update(CODEX_HOME=str(home), TMPDIR=str(native_temp), HOME=str(home),
                       TEST_ROOT=str(root), TEST_BASE_URL=route['proxy_base_url'],
                       TEST_SUPERVISOR_PID=str(supervisor_pid), TEST_AUTH_FD=str(auth_fd),
                       PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
            result = subprocess.run(wrap(['/usr/bin/python3', '-c', script],
                                         selected_context=selected_context), cwd=workspace,
                                    env=env, capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == {
                'auth_denied': True, 'proc_denied': True,
                'supervisor_rejected': selected_context, 'socket_blocked': not selected_context,
                'ptrace_denied': True, 'workspace_readable': True, 'management_env_absent': True,
            }
        finally:
            client.shutdown()


def test_retire_keeps_route_when_proxy_stop_cannot_be_verified(tmp_path, monkeypatch):
    supervisor = Supervisor(tmp_path / 'state/managed-proxies')
    account_dir = _account_dir(supervisor.directory, 'fixture-account')
    process = subprocess.Popen(['/usr/bin/python3', '-c', 'import time; time.sleep(30)'],
                               cwd=account_dir, start_new_session=True,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        record = {'version': 1, 'port': 18301, 'pid': process.pid,
                  'start': _pid_start(process.pid), 'binary': '/usr/bin/python3'}
        _write_record(account_dir, record)
        actual_getpgid = os.getpgid
        monkeypatch.setattr(os, 'getpgid', lambda pid: pid + 1)
        from agentbridge.errors import BridgeError
        with pytest.raises(BridgeError) as error:
            supervisor.route('retire', 'fixture-account')
        assert error.value.code == 'managed_proxy_stop_unverified'
        assert (account_dir / 'route.json').exists()
        assert process.poll() is None
        monkeypatch.setattr(os, 'getpgid', actual_getpgid)
        assert supervisor.route('retire', 'fixture-account')['retired'] is True
        assert process.wait(timeout=3) is not None
        assert not (account_dir / 'route.json').exists()
        assert (account_dir / 'stop-confirmed.json').exists()
        restarted = Supervisor(supervisor.directory)
        try:
            assert restarted.route('retire', 'fixture-account') == {
                'retired': True, 'upstream_credential_removed': False}
        finally:
            os.close(restarted.auth_fd)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)
        os.close(supervisor.auth_fd)


def test_retire_waits_for_sidecar_process_group_descendants(tmp_path):
    supervisor = Supervisor(tmp_path / 'state/managed-proxies')
    account_dir = _account_dir(supervisor.directory, 'fixture-account')
    child_code = ('import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                  'print("ready", flush=True); time.sleep(30)')
    leader_code = ('import subprocess,time; child=subprocess.Popen('
                   '["/usr/bin/python3", "-c", ' + repr(child_code) + '], '
                   'stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, '
                   'stderr=subprocess.DEVNULL, text=True); '
                   'child.stdout.readline(); print(child.pid, flush=True); time.sleep(30)')
    leader = subprocess.Popen(['/usr/bin/python3', '-c', leader_code],
                              cwd=account_dir, start_new_session=True,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True)
    child_pid = int(leader.stdout.readline())
    try:
        record = {'version': 1, 'port': 18302, 'pid': leader.pid,
                  'start': _pid_start(leader.pid), 'binary': '/usr/bin/python3'}
        _write_record(account_dir, record)
        assert supervisor.route('retire', 'fixture-account')['retired'] is True
        child_stat = Path(f'/proc/{child_pid}/stat')
        assert not child_stat.exists() or child_stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z'
        assert not (account_dir / 'route.json').exists()
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader.wait(timeout=3)
        os.close(supervisor.auth_fd)


def test_retire_does_not_attest_when_route_metadata_is_missing(tmp_path):
    from agentbridge.errors import BridgeError

    supervisor = Supervisor(tmp_path / 'state/managed-proxies')
    try:
        with pytest.raises(BridgeError) as error:
            supervisor.route('retire', 'fixture-account')
        assert error.value.code == 'managed_proxy_stop_unverified'
    finally:
        os.close(supervisor.auth_fd)


def test_route_record_write_failure_stops_the_spawned_sidecar(tmp_path, monkeypatch):
    import agentbridge.proxy.supervisor as supervisor_module

    supervisor = Supervisor(tmp_path / 'state/managed-proxies')
    account_dir = _account_dir(supervisor.directory, 'fixture-account')
    binary = tmp_path / 'fixture-cliproxy'
    binary.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(30)\n')
    binary.chmod(0o700)
    processes = []
    original_spawn = supervisor_module.subprocess.Popen

    def capture_spawn(*args, **kwargs):
        process = original_spawn(*args, **kwargs)
        processes.append(process)
        return process

    def fail_write(*_):
        raise OSError('fixture write failed')

    monkeypatch.setattr(supervisor_module.subprocess, 'Popen', capture_spawn)
    monkeypatch.setattr(supervisor_module, '_write_record', fail_write)
    try:
        with pytest.raises(OSError, match='fixture write failed'):
            supervisor_module._launch(binary, account_dir, 'fixture-account', 18303)
        assert len(processes) == 1 and processes[0].poll() is not None
        assert not (account_dir / 'route.json').exists()
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        os.close(supervisor.auth_fd)


def test_proxy_binary_does_not_resolve_a_workspace_entry_from_path(tmp_path, monkeypatch):
    from agentbridge.errors import BridgeError
    from agentbridge.proxy.supervisor import _binary

    fake = tmp_path / 'cliproxy'
    fake.write_text('#!/bin/sh\nexit 0\n')
    fake.chmod(0o700)
    monkeypatch.setenv('PATH', f'{tmp_path}:/usr/bin')
    monkeypatch.delenv('AGENTBRIDGE_CLIPROXY_BIN', raising=False)
    with pytest.raises(BridgeError) as error:
        _binary()
    assert error.value.code == 'proxy_binary_unavailable'
    monkeypatch.setenv('AGENTBRIDGE_CLIPROXY_BIN', 'cliproxy')
    with pytest.raises(BridgeError) as error:
        _binary()
    assert error.value.code == 'proxy_binary_unavailable'
    monkeypatch.setenv('AGENTBRIDGE_CLIPROXY_BIN', str(fake))
    assert _binary() == fake
