"""Route bindings reject credential replacement and duplicate upstream accounts."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
from threading import Thread

import pytest

from agentbridge import Account, Bridge
from agentbridge.errors import BridgeError
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account


@contextmanager
def management(identity, index, *, kind="oauth"):
    state = {"identity": identity, "index": index, "kind": kind}

    class Handler(BaseHTTPRequestHandler):
        def handle_get(self):
            if self.path == "/v0/management/auth-files":
                entry = {"name": "one.json", "auth_index": state["index"],
                         "account_type": state["kind"], "provider": "codex", "status": "active",
                         "disabled": False, "unavailable": False, "source": "file",
                         "runtime_only": False, "cooldowns": []}
                if state["kind"] == "oauth":
                    entry["id_token"] = {"chatgpt_account_id": state["identity"]}
                body = {"files": [entry]}
            elif self.path == "/v0/management/config":
                body = {name: [] for name in ("gemini-api-key", "interactions-api-key",
                    "claude-api-key", "codex-api-key", "xai-api-key", "meta-api-key",
                    "vertex-api-key", "openai-compatibility")}
                body["plugins"] = {"enabled": False}
            elif self.path == "/v0/management/auth-files/models?name=one.json":
                body = {"models": [{"id": "gpt-5"}]}
            else:
                self.send_error(404)
                return
            payload = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    setattr(Handler, "do_GET", Handler.handle_get)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, state
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def account(account_id, port):
    return Account(account_id, "codex", provider="codex", supported_models=("gpt-5",),
                   proxy_base_url=f"http://127.0.0.1:{port}/v1",
                   key_env="FIXTURE_PROXY_KEY", management_key_env="FIXTURE_MANAGEMENT_KEY")


def test_saved_proxy_without_grantbridge_login_stays_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_PROXY_KEY", secrets.token_hex(16))
    monkeypatch.setenv("FIXTURE_MANAGEMENT_KEY", secrets.token_hex(16))
    with management("private-account", "path-index") as (port, _):
        bridge = Bridge(tmp_path / "state")
        unlinked = account("old-route", port)
        with bridge.store.connect() as db:
            db.execute("INSERT INTO accounts(id,config) VALUES (?,?)",
                       (unlinked.id, json.dumps(unlinked.to_dict())))
        assert bridge.routes.observe(bridge.account("old-route")) is None
        assert bridge.store.proxy_binding("old-route") is None
        status = bridge.store.latest_account_observation("old-route")
        assert status["data"]["reason"] == "authentication_required"
        with pytest.raises(BridgeError) as error:
            bridge.routes.select("gpt-5")
        assert error.value.code == "provider_unavailable"


def test_changed_oauth_identity_at_same_path_is_rejected_before_next_route(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_PROXY_KEY", secrets.token_hex(16))
    monkeypatch.setenv("FIXTURE_MANAGEMENT_KEY", secrets.token_hex(16))
    with management("first-private-account", "same-path-index") as (port, state):
        bridge = Bridge(tmp_path / "state")
        seed_authenticated_proxy_account(bridge.store, account("route_a", port),
                                         observe_local=True)
        assert bridge.routes.select("gpt-5").account_id == "route_a"
        public_status = bridge.account_status("route_a")
        assert public_status["binding_verified"] is True
        assert "binding_fingerprint" not in repr(public_status)
        assert "identity_fingerprint" not in repr(public_status)
        state["identity"] = "replacement-private-account"
        with pytest.raises(BridgeError) as error:
            bridge.routes.select("gpt-5")
        assert error.value.code == "provider_unavailable"
        observation = bridge.store.latest_account_observation("route_a")
        assert observation["data"]["reason"] == "proxy_binding_changed"
        assert "binding_fingerprint" not in observation["data"]
        assert "identity_fingerprint" not in observation["data"]
        with bridge.store.connect() as db:
            rows = db.execute("SELECT * FROM proxy_bindings").fetchall()
        assert len(rows) == 1
        assert "private-account" not in repr(dict(rows[0]))
        assert "same-path-index" not in repr(dict(rows[0]))


def test_changed_oauth_identity_cannot_reuse_another_bound_route(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_PROXY_KEY", secrets.token_hex(16))
    monkeypatch.setenv("FIXTURE_MANAGEMENT_KEY", secrets.token_hex(16))
    with management("shared-private-account", "path-a") as (first, _):
        with management("different-private-account", "path-b") as (second, state):
            bridge = Bridge(tmp_path / "state")
            seed_authenticated_proxy_account(bridge.store, account("route_a", first),
                                             observe_local=True)
            seed_authenticated_proxy_account(bridge.store, account("route_b", second),
                                             observe_local=True)
            assert bridge.routes.observe(bridge.account("route_a")) is not None
            state["identity"] = "shared-private-account"
            assert bridge.routes.observe(bridge.account("route_b")) is None
            observation = bridge.store.latest_account_observation("route_b")
            assert observation["data"]["reason"] == "proxy_binding_changed"


def test_api_key_route_and_missing_oauth_identity_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_PROXY_KEY", secrets.token_hex(16))
    monkeypatch.setenv("FIXTURE_MANAGEMENT_KEY", secrets.token_hex(16))
    with management("unused", "first-key-index", kind="api_key") as (port, _):
        bridge = Bridge(tmp_path / "api-key-state")
        seed_authenticated_proxy_account(bridge.store, account("route_a", port))
        with pytest.raises(BridgeError) as error:
            bridge.routes.select("gpt-5")
        assert error.value.code == "provider_unavailable"
        assert bridge.store.latest_account_observation("route_a")["data"]["reason"] == "proxy_binding_unverified"
    with management("", "oauth-path-index") as (port, _):
        bridge = Bridge(tmp_path / "missing-identity-state")
        seed_authenticated_proxy_account(bridge.store, account("route_b", port))
        with pytest.raises(BridgeError) as error:
            bridge.routes.select("gpt-5")
        assert error.value.code == "provider_unavailable"
        assert bridge.store.latest_account_observation("route_b")["data"]["reason"] == "proxy_binding_unverified"
