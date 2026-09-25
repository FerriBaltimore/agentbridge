"""Exercise automatic route changes with deterministic local provider fixtures."""

from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

import pytest

from agentbridge import Account, Bridge, RunOptions
from agentbridge.errors import BridgeError
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from test_proxy_management import EMPTY_CONFIG, local_management


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def management_server(state):
    class Handler(BaseHTTPRequestHandler):
        def handle_get(self):
            if self.path == "/v0/management/auth-files":
                body = {"files": [{"name": "one.json", "provider": state.get("provider", "codex"),
                    "auth_index": state["identity"], "account_type": "oauth",
                    "id_token": {"chatgpt_account_id": state["identity"]},
                    "email": state["identity"] + "@fixture.invalid",
                    "source": "file", "runtime_only": False,
                    "status": "active", "disabled": False, "unavailable": False,
                    "cooldowns": [],
                    "quota": {"observed_at": _now(), "signals": {
                        "X-Codex-Primary-Used-Percent": str(state["used"])}}}]}
            elif self.path == "/v0/management/config":
                body = {name: [] for name in ("gemini-api-key", "interactions-api-key",
                    "claude-api-key", "codex-api-key", "xai-api-key", "meta-api-key",
                    "vertex-api-key", "openai-compatibility")}
                body["plugins"] = {"enabled": False}
            elif self.path == "/v0/management/auth-files/models?name=one.json":
                body = {"models": [{"id": model} for model in state.get("models", ["lab-model"])]}
            else:
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
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _fake_codex(path):
    source = Path(__file__).parent / 'fixtures/test_codex_session_provider.py'
    path.write_text('#!/usr/bin/env python3\n' + source.read_text())
    path.chmod(0o700)


def test_model_first_routing_switches_account_and_preserves_context(tmp_path, monkeypatch):
    fixture = tmp_path / "codex-fixture"
    _fake_codex(fixture)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state-home'))
    alpha, beta = {"used": 60, "identity": "fixture-alpha"}, {"used": 10, "identity": "fixture-beta"}
    with ExitStack() as stack:
        alpha_url = stack.enter_context(management_server(alpha))
        beta_url = stack.enter_context(management_server(beta))
        bridge = Bridge()
        assert bridge.root == tmp_path / 'state-home/agentbridge'
        for name, endpoint in (("alpha", alpha_url), ("beta", beta_url)):
            key_env = f"LAB_PROXY_{name.upper()}"
            management_env = f"LAB_MANAGEMENT_{name.upper()}"
            monkeypatch.setenv(key_env, f"local-fixture-{name}")
            monkeypatch.setenv(management_env, f"local-management-{name}")
            seed_authenticated_proxy_account(bridge.store, Account(name, "codex", name=name.title(),
                key_env=key_env, management_key_env=management_env,
                proxy_base_url=endpoint, provider="codex",
                supported_models=("lab-model",), command=(str(fixture),)),
                observe_local=True)

        catalog = bridge.models()
        assert catalog["models"][0]["id"] == "lab-model"
        instance = bridge.instance_create(model="lab-model", workspace_path=workspace)
        assert instance["routing_mode"] == "automatic"
        assert instance["account_ref"] == "Beta"

        first = bridge.message_create(instance["id"], "first")
        assert bridge.run(first["turn_id"]).wait(10)["state"] == "completed"
        assert json.loads(bridge.run(first["turn_id"]).text) == {
            "account": "beta", "resumed": False, "portable": False,
            "model": "lab-model", "native_id": "native-beta", "previous_prompts": []}

        alpha["used"], beta["used"] = 5, 90
        bridge.routes.observe(bridge.account("alpha"))
        bridge.routes.observe(bridge.account("beta"))
        steady = bridge.message_create(instance["id"], "stay on the warm account")
        assert bridge.run(steady["turn_id"]).wait(10)["state"] == "completed"
        assert steady["account_ref"] == "Beta"
        assert json.loads(bridge.run(steady["turn_id"]).text) == {
            "account": "beta", "resumed": True, "portable": False,
            "model": "lab-model", "native_id": "native-beta", "previous_prompts": ["first"]}
        steady_route = [event for event in bridge.turn_events(steady["turn_id"])
                        if event["kind"] == "route.selected"][0]["data"]
        assert steady_route["reason"] == "affinity"

        beta["used"] = 100
        bridge.routes.observe(bridge.account("beta"))
        second = bridge.message_create(instance["id"], "second")
        assert bridge.run(second["turn_id"]).wait(10)["state"] == "completed"
        assert second["account_ref"] == "Alpha"
        assert json.loads(bridge.run(second["turn_id"]).text) == {
            "account": "alpha", "resumed": True, "portable": False,
            "model": "lab-model", "native_id": "native-beta",
            "previous_prompts": ["first", "stay on the warm account"]}
        selected = [event for event in bridge.turn_events(second["turn_id"])
                    if event["kind"] == "route.selected"]
        assert selected[0]["data"]["account_changed"] is True
        assert selected[0]["data"]["portable_context_used"] is False
        assert bridge.instance_get(instance["id"])["native_session_id"] == "native-beta"
        assert selected[0]["data"]["affinity_break_reason"] == "quota_exhausted"

        beta["used"] = 0
        bridge.routes.observe(bridge.account("beta"))
        third = bridge.message_create(instance["id"], "third")
        assert bridge.run(third["turn_id"]).wait(10)["state"] == "completed"
        assert json.loads(bridge.run(third["turn_id"]).text) == {
            "account": "alpha", "resumed": True, "portable": False,
            "model": "lab-model", "native_id": "native-beta",
            "previous_prompts": ["first", "stay on the warm account", "second"]}

        original_admit = bridge.store.admit
        contested = {"occurred": False}

        def admit_with_competing_turn(*args, **kwargs):
            if not contested["occurred"] and kwargs.get("account_id") == "alpha":
                contested["occurred"] = True
                bridge.store.add_session("blocking-instance", "alpha", str(workspace), "lab-model")
                original_admit("blocking-turn", "blocking-instance", "other work",
                               RunOptions(model="lab-model"), None)
            return original_admit(*args, **kwargs)

        bridge.store.admit = admit_with_competing_turn
        with pytest.raises(BridgeError) as busy:
            bridge.message_create(instance["id"], "fourth")
        bridge.store.admit = original_admit
        assert busy.value.code == "account_busy"
        assert contested["occurred"] is True
        assert bridge.store.routing(instance["id"])["affinity_account_id"] == "alpha"
        assert bridge.store.session_run_count(instance["id"]) == 4
        bridge.store.finish("blocking-turn", "cancelled")

        pinned = bridge.instance_create(model="lab-model", account_ref="Alpha",
                                        workspace_path=workspace)
        pinned_first = bridge.message_create(pinned["id"], "pinned first")
        assert bridge.run(pinned_first["turn_id"]).wait(10)["state"] == "completed"
        alpha["identity"] = "replacement-alpha"
        with pytest.raises(BridgeError) as error:
            bridge.message_create(pinned["id"], "pinned second")
        assert error.value.code == "proxy_binding_unverified"
        assert bridge.store.session_run_count(pinned["id"]) == 1
        bridge.close()


def test_excluded_account_cannot_win_initial_or_turn_route(tmp_path, monkeypatch):
    fixture = tmp_path / 'codex-fixture'
    _fake_codex(fixture)
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    alpha = {'used': 1, 'identity': 'fixture-alpha'}
    beta = {'used': 80, 'identity': 'fixture-beta'}
    with ExitStack() as stack:
        endpoints = (stack.enter_context(management_server(alpha)),
                     stack.enter_context(management_server(beta)))
        bridge = Bridge(tmp_path / 'state')
        for name, endpoint in zip(('alpha', 'beta'), endpoints):
            key_env = f'LAB_PROXY_{name.upper()}'
            management_env = f'LAB_MANAGEMENT_{name.upper()}'
            monkeypatch.setenv(key_env, f'local-fixture-{name}')
            monkeypatch.setenv(management_env, f'local-management-{name}')
            seed_authenticated_proxy_account(bridge.store, Account(
                name, 'codex', name=' Alpha ' if name == 'alpha' else 'Beta', key_env=key_env,
                management_key_env=management_env, proxy_base_url=endpoint,
                provider='codex', supported_models=('lab-model',), command=(str(fixture),)),
                observe_local=True)

        new = bridge.instance_create(model='lab-model', workspace_path=workspace,
                                     excluded_account_refs=[' Alpha '])
        assert new['account_ref'] == 'Beta'
        existing = bridge.instance_create(model='lab-model', workspace_path=workspace)
        assert existing['account_ref'] == ' Alpha '
        turn = bridge.message_create(existing['id'], 'route around deletion',
                                     excluded_account_refs=[' Alpha '])
        route = [event for event in bridge.turn_events(turn['turn_id'])
                 if event['kind'] == 'route.selected'][0]['data']
        assert route['affinity_break_reason'] == 'explicit_exclusion'
        assert route['affinity_break_evidence'] == {'account_id': 'alpha'}
        assert turn['account_ref'] == 'Beta'
        assert bridge.run(turn['turn_id']).wait(10)['state'] == 'completed'
        with pytest.raises(BridgeError) as error:
            bridge.message_create(existing['id'], 'no remaining route',
                                  excluded_account_refs=[' Alpha ', 'Beta'])
        assert error.value.code == 'model_unavailable'
        bridge.close()


def test_model_controls_come_from_the_proxy_client_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv('FIXTURE_MANAGEMENT_KEY', 'management-fixture-value')
    monkeypatch.setenv('FIXTURE_CLIENT_KEY', 'client-fixture-value')
    responses = {
        '/v0/management/auth-files': (200, {'files': [{
            'name': 'one.json', 'provider': 'codex', 'auth_index': 'fixture-index',
            'account_type': 'oauth', 'id_token': {'chatgpt_account_id': 'fixture-id'},
            'source': 'file', 'runtime_only': False, 'status': 'active',
            'disabled': False, 'unavailable': False, 'cooldowns': [],
        }]}, {}),
        '/v0/management/config': (200, EMPTY_CONFIG, {}),
        '/v0/management/auth-files/models?name=one.json': (
            200, {'models': [{'id': 'fixture/model'}]}, {}),
        '/v1/models?client_version=pi': (200, {'models': [
            {'slug': 'fixture/model', 'context_window': 131072,
             'max_context_window': 262144,
             'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'high'},
                                            {'effort': 'private-level'}],
             'default_reasoning_level': 'high',
             'input_modalities': ['text', 'image', 'private'],
             'private_token': 'never-persist-this'},
            {'slug': 'other/model', 'context_window': 999999},
        ]}, {}),
    }
    with local_management(responses) as (port, seen):
        bridge = Bridge(tmp_path / 'state')
        account = Account('fixture', 'codex', name='Fixture', provider='codex',
                          supported_models=('fixture/model',),
                          proxy_base_url=f'http://127.0.0.1:{port}/v1',
                          key_env='FIXTURE_CLIENT_KEY',
                          management_key_env='FIXTURE_MANAGEMENT_KEY')
        seed_authenticated_proxy_account(bridge.store, account, observe_local=True)
        result = bridge.models(refresh=True)
        item = result['models'][0]
        assert item['id'] == 'fixture/model'
        assert item['reasoning_efforts'] == ['low', 'high']
        assert item['context_windows'] == [131072, 262144]
        assert item['default_context_window'] == 131072
        assert item['max_context_window'] == 262144
        assert item['input_modalities'] == ['text', 'image']
        assert item['account_capabilities'][0]['metadata_source'] == 'cliproxy_client_models'
        assert ('/v1/models?client_version=pi', 'Bearer client-fixture-value') in seen
        saved = bridge.store.latest_account_observation('fixture')['data']
        assert 'never-persist-this' not in repr(saved)
        assert 'other/model' not in repr(saved)
