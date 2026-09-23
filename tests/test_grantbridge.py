"""The local GrantBridge transport drives proxy OAuth without storing keys."""

import json
from pathlib import Path
import secrets
import sys

import pytest

from agentbridge import GrantBridgeClient
from agentbridge.errors import BridgeError


ADAPTER = Path(__file__).parent / 'fixtures' / 'test_grantbridge_adapter.py'


def test_proxy_oauth_transport_reconnects_without_persisting_management_key(tmp_path, monkeypatch):
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
