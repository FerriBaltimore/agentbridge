"""Managed sidecar lifecycle with a local fixture executable and no real account."""

import json
import os
from pathlib import Path
import signal
import socket
import struct
import sys
import textwrap
import time

import pytest

from agentbridge import Bridge
from agentbridge.proxy.managed import ManagedProxyClient, _socket_address
from agentbridge.proxy.management import ManagementClient
from agentbridge.proxy.route import ProxyRoute


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
