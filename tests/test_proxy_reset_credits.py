"""Fixture acceptance for Codex reset calls through one local proxy account."""

from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
from threading import Thread

import pytest

from agentbridge.errors import BridgeError
from agentbridge.proxy import ManagementClient, ProxyRoute
from agentbridge.proxy.reset_credits import CodexResetProxy, consume, read
from test_proxy_management import EMPTY_CONFIG


_CREDIT = {"id": "RateLimitResetCredit_fixture", "reset_type": "codex_rate_limits",
           "status": "available", "granted_at": "2026-09-24T10:00:00Z",
           "expires_at": None, "title": "Fixture reset", "description": "Fixture only",
           "private": "provider-private-field"}


@contextmanager
def reset_proxy(monkeypatch, *, count=2, details=None, consume_response=None):
    monkeypatch.setenv("FIXTURE_RESET_MANAGEMENT_KEY", secrets.token_hex(16))
    entry = {"name": "codex-fixture.json", "source": "file", "runtime_only": False,
             "provider": "codex", "status": "active", "disabled": False,
             "unavailable": True, "cooldowns": [], "auth_index": "opaque-fixture-index",
             "account_type": "oauth", "id_token": {"chatgpt_account_id": "acct-fixture"}}
    state = {"entry": entry, "calls": [], "usage": {
        "status_code": 200,
        "body": json.dumps({"account_id": "acct-fixture",
                            "rate_limit_reset_credits": {"available_count": count}})},
        "details": details if details is not None else {
            "status_code": 200, "body": json.dumps({"available_count": count,
                                                     "credits": [_CREDIT] if count else []})},
        "consume": consume_response if consume_response is not None else {
            "status_code": 200, "body": json.dumps({"code": "reset", "windows_reset": 1})}}

    class Handler(BaseHTTPRequestHandler):
        def handle_get(self):
            if self.path == "/v0/management/auth-files":
                payload = {"files": [state["entry"]]}
            elif self.path == "/v0/management/config":
                payload = EMPTY_CONFIG
            elif self.path == "/v0/management/auth-files/models?name=codex-fixture.json":
                payload = {"models": [{"id": "gpt-fixture"}]}
            else:
                self.send_error(404)
                return
            self._send(payload)

        def handle_post(self):
            if self.path != "/v0/management/api-call":
                self.send_error(404)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = json.loads(body)
            state["calls"].append(request)
            suffix = request["url"].removeprefix("https://chatgpt.com/backend-api/wham/")
            key = {"usage": "usage", "rate-limit-reset-credits": "details",
                   "rate-limit-reset-credits/consume": "consume"}.get(suffix)
            if key == "consume" and state.get("swap_after_consume"):
                state["entry"]["id_token"] = {"chatgpt_account_id": "different-account"}
            self._send(state[key] if key else {"status_code": 404, "body": "{}"})

        def _send(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):
            pass

    setattr(Handler, "do_GET", Handler.handle_get)
    setattr(Handler, "do_POST", Handler.handle_post)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        route = ProxyRoute("fixture", f"http://127.0.0.1:{server.server_port}/v1",
                           "FIXTURE_RESET_CLIENT_KEY")
        client = ManagementClient(route, "FIXTURE_RESET_MANAGEMENT_KEY")
        binding = client.observe()["binding_fingerprint"]
        yield client, binding, state
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_read_counts_details_and_keeps_provider_body_private(monkeypatch):
    with reset_proxy(monkeypatch) as (client, binding, state):
        result = read(client, binding)
    assert result["available_count"] == 2
    assert result["status"] == "available"
    assert result["credits"] == [{
        "id": "RateLimitResetCredit_fixture", "reset_type": "codex_rate_limits",
        "status": "available", "granted_at": "2026-09-24T10:00:00Z",
        "expires_at": None, "title": "Fixture reset", "description": "Fixture only"}]
    assert datetime.fromisoformat(result["observed_at"].replace("Z", "+00:00")).tzinfo == timezone.utc
    assert "provider-private-field" not in repr(result)
    assert [call["method"] for call in state["calls"]] == ["GET", "GET"]
    assert [call["url"] for call in state["calls"]] == [
        "https://chatgpt.com/backend-api/wham/usage",
        "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"]
    assert all(call["auth_index"] == "opaque-fixture-index" for call in state["calls"])
    assert all(call["header"]["Authorization"] == "Bearer $TOKEN$"
               and call["header"]["ChatGPT-Account-Id"] == "acct-fixture"
               for call in state["calls"])


def test_read_preserves_count_when_detail_endpoint_unavailable(monkeypatch):
    unavailable = {"status_code": 503, "body": "private upstream error"}
    with reset_proxy(monkeypatch, count=3, details=unavailable) as (client, binding, _):
        result = CodexResetProxy(client, binding).read()
    assert result["available_count"] == 3
    assert result["status"] == "available"
    assert result["credits"] is None
    assert "private" not in repr(result)


def test_read_distinguishes_zero_and_missing_count(monkeypatch):
    with reset_proxy(monkeypatch, count=0) as (client, binding, _):
        result = CodexResetProxy(client, binding).read()
    assert result["available_count"] == 0
    assert result["status"] == "none" and result["credits"] == []
    unavailable = {"status_code": 401, "body": "private auth error"}
    with reset_proxy(monkeypatch, details=unavailable) as (client, binding, state):
        state["usage"] = unavailable
        with pytest.raises(BridgeError) as error:
            CodexResetProxy(client, binding).read()
    assert error.value.code == "upstream_reset_unavailable"
    assert "private" not in str(error.value)


def test_consume_uses_exact_backend_contract_and_stable_outcome(monkeypatch):
    with reset_proxy(monkeypatch) as (client, binding, state):
        result = consume(client, binding, "d24b89c0-91ca-4870-8882-698181bf4871",
                         "RateLimitResetCredit_fixture")
    assert result == {"outcome": "reset", "windows_reset": 1}
    assert len(state["calls"]) == 1
    request = state["calls"][0]
    assert request["method"] == "POST"
    assert request["url"] == (
        "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume")
    assert request["header"]["Authorization"] == "Bearer $TOKEN$"
    assert request["header"]["Content-Type"] == "application/json"
    assert json.loads(request["data"]) == {
        "redeem_request_id": "d24b89c0-91ca-4870-8882-698181bf4871",
        "credit_id": "RateLimitResetCredit_fixture"}


@pytest.mark.parametrize("code,expected", [
    ("already_redeemed", "already_redeemed"),
    ("nothing_to_reset", "nothing_to_reset"),
    ("no_credit", "no_credit"),
])
def test_consume_normalizes_known_outcomes_without_inventing_windows(monkeypatch, code, expected):
    response = {"status_code": 200, "body": json.dumps({"code": code})}
    with reset_proxy(monkeypatch, consume_response=response) as (client, binding, state):
        result = CodexResetProxy(client, binding).consume("logical-attempt")
    assert result == {"outcome": expected}
    assert json.loads(state["calls"][0]["data"]) == {"redeem_request_id": "logical-attempt"}


@pytest.mark.parametrize("response", [
    {"status_code": 503, "body": "private error"},
    {"status_code": 200, "body": "not-json private error"},
    {"status_code": 200, "body": json.dumps({"code": "future_code"})},
    {"status_code": 200, "body": json.dumps({"code": "reset", "windows_reset": "1"})},
])
def test_consume_preserves_unknown_outcome_after_submission(monkeypatch, response):
    with reset_proxy(monkeypatch, consume_response=response) as (client, binding, state):
        with pytest.raises(BridgeError) as error:
            CodexResetProxy(client, binding).consume("same-logical-attempt")
    assert error.value.code == "reset_outcome_unknown"
    assert error.value.outcome == "unknown"
    assert error.value.retryable is False
    assert len(state["calls"]) == 1
    assert "private" not in str(error.value)


def test_preflight_rejects_changed_identity_even_when_account_is_exhausted(monkeypatch):
    with reset_proxy(monkeypatch) as (client, binding, state):
        state["entry"]["id_token"] = {"chatgpt_account_id": "different-account"}
        with pytest.raises(BridgeError) as error:
            CodexResetProxy(client, binding).consume("logical-attempt")
    assert error.value.code == "proxy_binding_changed"
    assert error.value.outcome == "not_started"
    assert state["calls"] == []


def test_invalid_input_never_reaches_proxy(monkeypatch):
    with reset_proxy(monkeypatch) as (client, binding, state):
        with pytest.raises(BridgeError) as error:
            CodexResetProxy(client, binding).consume("bad key")
        with pytest.raises(BridgeError) as second:
            CodexResetProxy(client, binding).consume("valid-key", "")
    assert error.value.code == "invalid_idempotency_key"
    assert second.value.code == "invalid_credit_id"
    assert state["calls"] == []


def test_postflight_identity_change_leaves_redemption_unknown(monkeypatch):
    with reset_proxy(monkeypatch) as (client, binding, state):
        state["swap_after_consume"] = True
        with pytest.raises(BridgeError) as error:
            CodexResetProxy(client, binding).consume("logical-attempt")
    assert error.value.code == "reset_outcome_unknown"
    assert error.value.outcome == "unknown"
    assert len(state["calls"]) == 1


def test_transport_failure_after_admission_leaves_redemption_unknown(monkeypatch):
    with reset_proxy(monkeypatch) as (client, binding, state):
        def fail_after_send(*args, **kwargs):
            raise BridgeError("proxy_observation_unavailable", "Synthetic transport failure.")
        monkeypatch.setattr(client, "_post_json", fail_after_send)
        with pytest.raises(BridgeError) as error:
            CodexResetProxy(client, binding).consume("logical-attempt")
    assert error.value.code == "reset_outcome_unknown"
    assert error.value.outcome == "unknown"
    assert state["calls"] == []
