"""The local GrantBridge transport drives proxy OAuth without storing keys."""

import json
from pathlib import Path
import secrets
import shutil
import socket
import sys

import pytest

from agentbridge import GrantBridgeClient
from agentbridge.errors import BridgeError


ADAPTER = Path(__file__).parent / 'fixtures' / 'test_grantbridge_adapter.py'


def test_aa_auth_proxy_proof_passes_browser_socket_to_protected_node(tmp_path, monkeypatch):
    node = shutil.which('node')
    if node is None:
        pytest.skip('A Node runtime is required for the GrantBridge launch regression.')
    adapter = tmp_path / 'agentbridge-proxy-adapter.mjs'
    adapter.write_text('''
import { createInterface } from 'node:readline';
const input = createInterface({ input: process.stdin });
for await (const line of input) {
  const request = JSON.parse(line);
  const result = {
    flags: process.execArgv,
    arguments: process.argv.slice(2),
    socket: process.env.GRANTBRIDGE_BROWSER_SOCKET,
    proxy: process.env.GRANTBRIDGE_BROWSER_PROXY,
    sandbox: process.env.GRANTBRIDGE_BROWSER_SANDBOX,
  };
  console.log(JSON.stringify({ jsonrpc: '2.0', id: request.id, result }));
}
''')
    monkeypatch.setenv('GRANTBRIDGE_BROWSER_SOCKET', '/run/fullbrain-browser.sock')
    monkeypatch.setenv('GRANTBRIDGE_BROWSER_PROXY', 'http://127.0.0.1:18080')
    monkeypatch.setenv('GRANTBRIDGE_BROWSER_SANDBOX', 'external')
    data_dir = tmp_path / 'adapter-state'
    with GrantBridgeClient(adapter=adapter, node=node, data_dir=data_dir) as client:
        result = client._request('fixture.launch')
        assert client.alive
    assert result['flags'] == ['--disable-sigusr1']
    assert result['arguments'] == ['--data-dir', str(data_dir)]
    assert result['socket'] == '/run/fullbrain-browser.sock'
    assert result['proxy'] == 'http://127.0.0.1:18080'
    assert result['sandbox'] == 'external'


def test_proxy_oauth_transport_reconnects_without_persisting_management_key(tmp_path, monkeypatch):
    monkeypatch.setattr('agentbridge.grantbridge.ensure_callback_port_available',
                        lambda provider: None)
    secret = secrets.token_hex(24)
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secret)
    data_dir = tmp_path / 'adapter'
    with GrantBridgeClient(adapter=ADAPTER, node=sys.executable, data_dir=data_dir) as client:
        config = client.configuration()
        started = client.proxy_start('codex', 'http://127.0.0.1:8317/v1', 'LAB_MANAGEMENT_KEY')
    assert started['status'] == 'awaiting_user'
    assert secret not in json.dumps(config) + json.dumps(started)

    (data_dir / 'authorize').touch()
    with GrantBridgeClient(**config) as resumed:
        authorized = resumed.proxy_status(started['id'], 'codex',
                                          'http://127.0.0.1:8317/v1', 'LAB_MANAGEMENT_KEY')
        cancelled = resumed.proxy_cancel(started['id'], 'codex',
                                         'http://127.0.0.1:8317/v1', 'LAB_MANAGEMENT_KEY')
    assert authorized['status'] == 'authorized'
    assert cancelled['status'] == 'cancelled'
    assert secret not in (data_dir / 'proxy-attempt.json').read_text()


def test_proxy_transport_rejects_missing_management_key_before_launch(tmp_path, monkeypatch):
    monkeypatch.delenv('LAB_MANAGEMENT_KEY', raising=False)
    data_dir = tmp_path / 'adapter'
    with GrantBridgeClient(adapter=ADAPTER, node=sys.executable, data_dir=data_dir) as client:
        with pytest.raises(BridgeError) as error:
            client.proxy_start('codex', 'http://127.0.0.1:8317/v1', 'LAB_MANAGEMENT_KEY')
        assert client.process is None
    assert error.value.code == 'credential_unavailable'
    assert not data_dir.exists()


def test_busy_callback_port_rejects_start_before_adapter_launch(tmp_path, monkeypatch):
    monkeypatch.setenv('LAB_MANAGEMENT_KEY', secrets.token_hex(24))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        monkeypatch.setattr('agentbridge.auth_callback._DESTINATIONS',
                            {'codex': (port, '/auth/callback')})
        with GrantBridgeClient(adapter=ADAPTER, node=sys.executable,
                               data_dir=tmp_path / 'adapter') as client:
            with pytest.raises(BridgeError) as error:
                client.proxy_start('codex', 'http://127.0.0.1:8317/v1', 'LAB_MANAGEMENT_KEY')
            assert error.value.code == 'oauth_callback_port_busy'
            assert client.process is None
    assert not (tmp_path / 'adapter').exists()
