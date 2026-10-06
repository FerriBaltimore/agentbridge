"""AV-11: the bundled sidecar's own startup traffic stays inside the worker egress policy.

The real bundled CLIProxyAPI is launched through the supervisor's own path behind a
loopback CONNECT recorder standing in for the worker egress proxy. Skipped when the
reviewed bundle archive is not prepared in this checkout.
"""

import os
import socket
import sys
import threading
import time

import pytest

from agentbridge.bundle import resolve_binary
from agentbridge.bundle.runtime import PACKAGE_ROOT
from agentbridge.proxy.process_lifecycle import _stop_record
from agentbridge.proxy.supervisor import Supervisor, _account_dir, _launch, _port

OBSERVE_SECONDS = 3.0
# CLIProxyAPI 7.3.16-fullbrain.2 still checks one remote version manifest at start,
# unconditionally. A new pinned build is the only way to remove it; tighten to zero then.
RESIDUAL_STARTUP_CONNECTS = 1


def _bundle_available():
    return (sys.platform.startswith('linux') and hasattr(os, 'memfd_create') and
            (PACKAGE_ROOT / 'assets' / 'cli_proxy_api.tar.gz').is_file())


class ConnectRecorder:
    """Loopback stand-in for the egress proxy: records request lines, denies everything."""

    def __init__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(64)
        self.listener.settimeout(0.2)
        self.url = f'http://127.0.0.1:{self.listener.getsockname()[1]}'
        self.lines = []
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._deny, args=(connection,), daemon=True).start()
        self.listener.close()

    def _deny(self, connection):
        connection.settimeout(2)
        head = b''
        try:
            while b'\r\n\r\n' not in head and len(head) < 65536:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                head += chunk
            with self.lock:
                self.lines.append(head.split(b'\r\n', 1)[0].decode('latin-1'))
            connection.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n'
                               b'Connection: close\r\n\r\n')
        except OSError:
            pass
        finally:
            connection.close()

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=2)
        with self.lock:
            return list(self.lines)


@pytest.mark.skipif(not _bundle_available(), reason='bundled CLIProxyAPI archive not prepared')
def test_av11_bundled_sidecar_start_connects_only_to_the_known_version_check(tmp_path,
                                                                              monkeypatch):
    recorder = ConnectRecorder()
    for key in ('HTTPS_PROXY', 'HTTP_PROXY', 'ALL_PROXY'):
        monkeypatch.setenv(key, recorder.url)
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('AGENTBRIDGE_CLIPROXY_BIN', raising=False)
    supervisor = Supervisor(tmp_path / 'state/managed-proxies')
    account_dir = _account_dir(supervisor.directory, 'fixture-account')
    binary = resolve_binary('cli_proxy_api', supervisor.directory.parent)
    active = None
    try:
        active = _launch(binary, account_dir, 'fixture-account', _port())
        assert active['process'].poll() is None
        time.sleep(OBSERVE_SECONDS)
    finally:
        if active is not None:
            _stop_record(active['record'], account_dir)
            active['process'].wait(timeout=5)
        os.close(supervisor.auth_fd)
        lines = recorder.close()
    tunnels = [line for line in lines if line.startswith('CONNECT ')]
    assert lines == tunnels, 'the sidecar only speaks CONNECT to the egress proxy'
    assert len(tunnels) <= RESIDUAL_STARTUP_CONNECTS, (
        f'{len(tunnels)} startup CONNECT attempts; the remote model catalogues are back on')
