"""A deterministic local proxy account for process supervision tests."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

from agentbridge import Account, Bridge
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account


MODEL = "fixture-model"
CLIENT_KEY_ENV = "FIXTURE_PROXY_KEY"
MANAGEMENT_KEY_ENV = "FIXTURE_MANAGEMENT_KEY"


@contextmanager
def management_server(*, model=MODEL):
    """Expose one static account through the bounded local Management API."""
    config = {key: [] for key in (
        "gemini-api-key", "interactions-api-key", "claude-api-key", "codex-api-key",
        "xai-api-key", "meta-api-key", "vertex-api-key", "openai-compatibility",
    )}
    config["plugins"] = {"enabled": False}
    responses = {
        "/v0/management/auth-files": {"files": [{
            "name": "fixture.json", "source": "file", "runtime_only": False,
            "provider": "codex", "status": "active", "disabled": False,
            "unavailable": False, "cooldowns": [], "account_type": "oauth",
            "auth_index": "fixture-auth-index",
            "id_token": {"chatgpt_account_id": "fixture-account-id"},
        }]},
        "/v0/management/config": config,
        "/v0/management/auth-files/models?name=fixture.json": {
            "models": [{"id": model}]},
    }

    class Handler(BaseHTTPRequestHandler):
        def handle_get(self):
            body = responses.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, format, *args):
            pass

    setattr(Handler, "do_GET", Handler.handle_get)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def bridge_with_proxy(tmp_path, monkeypatch, port, *, command=(), model=MODEL):
    """Seed an account through the internal store for execution tests."""
    monkeypatch.setenv(CLIENT_KEY_ENV, "fixture-client-key")
    monkeypatch.setenv(MANAGEMENT_KEY_ENV, "fixture-management-key")
    bridge = Bridge(tmp_path.parent / f'{tmp_path.name}-state')
    seed_authenticated_proxy_account(bridge.store, Account(
        "fixture", "codex", provider="codex", supported_models=(model,),
        proxy_base_url=f"http://127.0.0.1:{port}/v1",
        key_env=CLIENT_KEY_ENV, management_key_env=MANAGEMENT_KEY_ENV,
        command=command,
    ), observe_local=True)
    bridge.routes.observe(bridge.account("fixture"))
    return bridge
