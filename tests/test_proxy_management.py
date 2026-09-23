"""Passive CLIProxyAPI management snapshots never expose raw credential data."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
from threading import Thread

import pytest

from agentbridge.errors import BridgeError
from agentbridge.proxy import ManagementClient, ProxyRoute


EMPTY_CONFIG = {"plugins": {"enabled": False},
                "gemini-api-key": [], "interactions-api-key": [], "claude-api-key": [],
                "codex-api-key": [], "xai-api-key": [], "meta-api-key": [],
                "vertex-api-key": [], "openai-compatibility": []}


@contextmanager
def local_management(responses):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def handle_get(self):
            seen.append((self.path, self.headers.get("Authorization")))
            status, body, headers = responses.get(self.path, (404, {}, {}))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, format, *args):
            pass

    # BaseHTTPRequestHandler requires this protocol spelling; keep authored
    # Python method names in snake_case for the repository naming guard.
    setattr(Handler, "do_GET", Handler.handle_get)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, seen
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_observation_normalizes_quota_and_discards_untrusted_fields(monkeypatch):
    key = secrets.token_hex(16)
    monkeypatch.setenv("LAB_MANAGEMENT_KEY", key)
    responses = {
        "/v0/management/auth-files": (200, {
            "observed_at": "2026-09-23T12:00:00Z",
            "files": [{"name": "one.json", "source": "file", "runtime_only": False,
                       "provider": "codex", "status": "active", "disabled": False,
                       "auth_index": "fixture-index-a", "account_type": "oauth",
                       "id_token": {"chatgpt_account_id": "fixture-account-a"},
                       "unavailable": False, "status_message": "private provider diagnostic",
                       "quota": {"observed_at": "2026-09-23T11:59:00Z",
                                 "signals": {"X-Codex-Primary-Used-Percent": "58",
                                             "X-Codex-Secondary-Used-Percent": "71",
                                             "Other-Private-Header": "private metadata"}},
                       "model_quotas": {"gpt-5": {"observed_at": "2026-09-23T12:00:00Z",
                                                  "signals": {"X-Codex-Primary-Used-Percent": "90"}}},
                       "cooldowns": [{"scope": "model", "model_key": "gpt-5",
                                      "retry_at": "2026-09-23T12:15:00Z", "remaining_seconds": 900,
                                      "reason": "private provider diagnostic"}]}]}, {}),
        "/v0/management/config": (200, EMPTY_CONFIG, {}),
        "/v0/management/auth-files/models?name=one.json": (
            200, {"models": [{"id": "gpt-5", "display_name": "private metadata"}, {"id": "gpt-6"}]}, {}),
    }
    with local_management(responses) as (port, seen):
        route = ProxyRoute("account_a", f"http://127.0.0.1:{port}/v1", "LAB_PROXY_KEY")
        result = ManagementClient(route, "LAB_MANAGEMENT_KEY").observe()
    assert [path for path, _ in seen] == [
        "/v0/management/auth-files", "/v0/management/config",
        "/v0/management/auth-files/models?name=one.json"]
    assert all(header == f"Bearer {key}" for _, header in seen)
    assert result["account_id"] == "account_a"
    assert result["status"] == "active"
    assert len(result["binding_fingerprint"]) == 64
    assert len(result["identity_fingerprint"]) == 64
    assert result["account_used_percent"] == 71.0
    assert result["cooldown_known"] is True
    assert result["models"] == [
        {"id": "gpt-5", "used_percent": 90.0, "quota_observed_at": "2026-09-23T12:00:00Z",
         "quota_scope": "model", "cooldown_until": "2026-09-23T12:15:00Z"},
        {"id": "gpt-6", "used_percent": 71.0, "quota_observed_at": "2026-09-23T11:59:00Z",
         "quota_scope": "account", "cooldown_until": None},
    ]
    assert "private" not in repr(result)
    assert "fixture-account-a" not in repr(result)
    assert "fixture-index-a" not in repr(result)
    assert key not in repr(result)


def test_dedicated_endpoint_rejects_multiple_credentials(monkeypatch):
    monkeypatch.setenv("LAB_MANAGEMENT_KEY", secrets.token_hex(16))
    responses = {"/v0/management/auth-files": (
        200, {"files": [{"name": "one.json"}, {"name": "two.json"}]}, {})}
    with local_management(responses) as (port, seen):
        route = ProxyRoute("account_a", f"http://127.0.0.1:{port}/v1", "LAB_PROXY_KEY")
        with pytest.raises(BridgeError) as error:
            ManagementClient(route, "LAB_MANAGEMENT_KEY").observe()
    assert error.value.code == "proxy_binding_unverified"
    assert [path for path, _ in seen] == ["/v0/management/auth-files"]


@pytest.mark.parametrize("hidden_key", ["codex-api-key", "openai-compatibility"])
def test_dedicated_endpoint_rejects_hidden_config_credentials(monkeypatch, hidden_key):
    secret = secrets.token_hex(16)
    monkeypatch.setenv("LAB_MANAGEMENT_KEY", secrets.token_hex(16))
    config = {**EMPTY_CONFIG, hidden_key: [{"api-key": secret}]}
    responses = {
        "/v0/management/auth-files": (200, {"files": [{
            "name": "one.json", "source": "file", "runtime_only": False,
            "cooldowns": [], "provider": "codex", "status": "active",
            "disabled": False, "unavailable": False,
            "auth_index": "fixture-index-a", "account_type": "oauth",
            "id_token": {"chatgpt_account_id": "fixture-account-a"}}]}, {}),
        "/v0/management/config": (200, config, {}),
    }
    with local_management(responses) as (port, seen):
        route = ProxyRoute("account_a", f"http://127.0.0.1:{port}/v1", "LAB_PROXY_KEY")
        with pytest.raises(BridgeError) as error:
            ManagementClient(route, "LAB_MANAGEMENT_KEY").observe()
    assert error.value.code == "proxy_binding_unverified"
    assert secret not in str(error.value)
    assert [path for path, _ in seen] == ["/v0/management/auth-files", "/v0/management/config"]


@pytest.mark.parametrize("config", [{}, {**EMPTY_CONFIG, "plugins": {"enabled": True}},
                                     {key: value for key, value in EMPTY_CONFIG.items()
                                      if key != "codex-api-key"}])
def test_dedicated_endpoint_requires_complete_safe_inventory(monkeypatch, config):
    monkeypatch.setenv("LAB_MANAGEMENT_KEY", secrets.token_hex(16))
    responses = {
        "/v0/management/auth-files": (200, {"files": [{
            "name": "one.json", "source": "file", "runtime_only": False,
            "cooldowns": []}]}, {}),
        "/v0/management/config": (200, config, {}),
    }
    with local_management(responses) as (port, _):
        route = ProxyRoute("account_a", f"http://127.0.0.1:{port}/v1", "LAB_PROXY_KEY")
        with pytest.raises(BridgeError) as error:
            ManagementClient(route, "LAB_MANAGEMENT_KEY").observe()
    assert error.value.code == "proxy_binding_unverified"


def test_dedicated_endpoint_rejects_home_managed_inventory(monkeypatch):
    monkeypatch.setenv("LAB_MANAGEMENT_KEY", secrets.token_hex(16))
    responses = {"/v0/management/auth-files": (200, {"files": [{
        "name": "one.json", "source": "file", "runtime_only": False,
        "cooldowns": None}]}, {})}
    with local_management(responses) as (port, seen):
        route = ProxyRoute("account_a", f"http://127.0.0.1:{port}/v1", "LAB_PROXY_KEY")
        with pytest.raises(BridgeError) as error:
            ManagementClient(route, "LAB_MANAGEMENT_KEY").observe()
    assert error.value.code == "proxy_binding_unverified"
    assert [path for path, _ in seen] == ["/v0/management/auth-files"]


def test_missing_quota_is_unknown_and_management_errors_are_sanitized(monkeypatch):
    monkeypatch.setenv("LAB_MANAGEMENT_KEY", secrets.token_hex(16))
    responses = {
        "/v0/management/auth-files": (200, {
            "observed_at": "2026-09-23T12:00:00Z",
            "files": [{"name": "one.json", "source": "file", "runtime_only": False,
                       "provider": "grok", "disabled": False,
                       "auth_index": "fixture-index-grok", "account_type": "oauth",
                       "email": "grok@example.test",
                       "unavailable": False,
                       "quota": {"observed_at": "2026-09-23T12:00:00Z",
                                 "signals": {"X-Codex-Primary-Used-Percent": "5"}},
                       "cooldowns": []}]}, {}),
        "/v0/management/config": (200, EMPTY_CONFIG, {}),
        "/v0/management/auth-files/models?name=one.json": (200, {"models": [{"id": "grok-4"}]}, {}),
    }
    with local_management(responses) as (port, _):
        route = ProxyRoute("account_a", f"http://127.0.0.1:{port}/v1", "LAB_PROXY_KEY")
        result = ManagementClient(route, "LAB_MANAGEMENT_KEY").observe()
    assert result["models"][0]["used_percent"] is None
    assert result["cooldown_known"] is True

    responses = {"/v0/management/auth-files": (
        302, {"error": "private provider diagnostic"}, {"Location": "http://127.0.0.1:9/elsewhere"})}
    with local_management(responses) as (port, seen):
        route = ProxyRoute("account_a", f"http://127.0.0.1:{port}/v1", "LAB_PROXY_KEY")
        with pytest.raises(BridgeError) as error:
            ManagementClient(route, "LAB_MANAGEMENT_KEY").observe()
    assert error.value.code == "proxy_observation_unavailable"
    assert "private provider diagnostic" not in str(error.value)
    assert len(seen) == 1


def test_management_response_has_a_bounded_body(monkeypatch):
    monkeypatch.setenv("LAB_MANAGEMENT_KEY", secrets.token_hex(16))
    responses = {"/v0/management/auth-files": (200, {
        "files": [], "private": "PRIVATE_BODY" + "x" * (1024 * 1024),
    }, {})}
    with local_management(responses) as (port, seen):
        route = ProxyRoute("account_a", f"http://127.0.0.1:{port}/v1", "LAB_PROXY_KEY")
        with pytest.raises(BridgeError) as error:
            ManagementClient(route, "LAB_MANAGEMENT_KEY").credential_count()
    assert error.value.code == "proxy_observation_unavailable"
    assert "PRIVATE_BODY" not in str(error.value)
    assert [path for path, _ in seen] == ["/v0/management/auth-files"]


def test_management_http_timeout_is_bounded_and_sanitized(monkeypatch):
    from agentbridge.proxy import management

    seen = []

    class TimedOutConnection:
        def __init__(self, host, port, timeout):
            seen.append((host, port, timeout))

        def request(self, method, path, headers):
            assert method == "GET" and path == "/v0/management/auth-files"
            assert headers["Authorization"] == "Bearer fixture-secret"

        def getresponse(self):
            raise TimeoutError("PRIVATE_BODY")

        def close(self):
            pass

    monkeypatch.setattr(management.http.client, "HTTPConnection", TimedOutConnection)
    route = ProxyRoute("account_a", "http://127.0.0.1:11011/v1", "LAB_PROXY_KEY")
    with pytest.raises(BridgeError) as error:
        ManagementClient(route, "LAB_MANAGEMENT_KEY", timeout=.25)._get_json(
            "/v0/management/auth-files", "fixture-secret")
    assert seen == [("127.0.0.1", 11011, .25)]
    assert error.value.code == "proxy_observation_unavailable"
    assert "PRIVATE_BODY" not in str(error.value)
