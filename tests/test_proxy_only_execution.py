"""Execution gates reject historical direct accounts before a process starts."""

from dataclasses import asdict
import io
import json
import sys
import time
from types import SimpleNamespace

import pytest

from agentbridge import Account, RunOptions
from agentbridge import worker
from agentbridge.errors import BridgeError
from agentbridge.routing import RouteDecision
from agentbridge.store import Store
from agentbridge.transports import command
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account


MODEL = "fixture-model"


def _legacy_account(store, tmp_path, engine):
    config = {"id": "legacy", "engine": engine, "home": str(tmp_path / "native-home")}
    with store.connect() as db:
        db.execute("INSERT INTO accounts(id,config) VALUES (?,?)", ("legacy", json.dumps(config)))


def _historical_session(store, tmp_path, *, mode="pinned"):
    now = time.time()
    with store.connect() as db:
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                   ("historical", "legacy", str(tmp_path), MODEL, None, None, None, now))
        db.execute("INSERT INTO instance_metadata VALUES (?,?,?,?)",
                   ("historical", "active", 1, now))
        db.execute("INSERT INTO session_routing"
                   "(session_id,mode,last_completed_account_id,last_native_id)"
                   " VALUES (?,?,?,?)",
                   ("historical", mode, "legacy" if mode == "pinned" else None, None))


@pytest.mark.parametrize("engine", ("codex", "claude"))
def test_new_session_rejects_direct_accounts_but_keeps_old_rows_readable(tmp_path, engine):
    store = Store(tmp_path / "state")
    _legacy_account(store, tmp_path, engine)
    with pytest.raises(BridgeError) as error:
        store.add_session("new", "legacy", str(tmp_path), MODEL)
    assert error.value.code == "invalid_proxy_account"
    _historical_session(store, tmp_path)
    assert store.get("sessions", "historical")["model"] == MODEL


@pytest.mark.parametrize("engine", ("codex", "claude"))
def test_direct_account_cannot_admit_a_new_turn_on_historical_session(tmp_path, engine):
    store = Store(tmp_path / "state")
    _legacy_account(store, tmp_path, engine)
    _historical_session(store, tmp_path)
    with pytest.raises(BridgeError) as error:
        store.admit("new", "historical", "hello", RunOptions(), None)
    assert error.value.code == "invalid_proxy_account"
    assert store.session_run_count("historical") == 0


def test_automatic_route_cannot_select_a_direct_account(tmp_path):
    store = Store(tmp_path / "state")
    _legacy_account(store, tmp_path, "codex")
    _historical_session(store, tmp_path, mode="automatic")
    decision = RouteDecision("legacy", MODEL, "unknown", None, "healthy", 0, "quota_unknown")
    with pytest.raises(BridgeError) as error:
        store.admit("new", "historical", "hello", RunOptions(model=MODEL), None,
                    account_id="legacy", route_decision=decision)
    assert error.value.code == "invalid_proxy_account"
    assert store.session_run_count("historical") == 0


def test_unverified_proxy_cannot_create_or_admit_a_session(tmp_path):
    store = Store(tmp_path / "state")
    account = Account("proxy", "codex", provider="openai", supported_models=(MODEL,),
                      proxy_base_url="http://127.0.0.1:8317/v1", key_env="CLIENT_KEY",
                      management_key_env="MANAGEMENT_KEY")
    with store.connect() as db:
        db.execute("INSERT INTO accounts(id,config) VALUES (?,?)",
                   (account.id, json.dumps(account.to_dict())))
    with pytest.raises(BridgeError) as error:
        store.add_session("new", "proxy", str(tmp_path), MODEL)
    assert error.value.code == "proxy_binding_unverified"
    now = time.time()
    with store.connect() as db:
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                   ("historical", "proxy", str(tmp_path), MODEL, None, None, None, now))
        db.execute("INSERT INTO instance_metadata VALUES (?,?,?,?)",
                   ("historical", "active", 1, now))
        db.execute("INSERT INTO session_routing"
                   "(session_id,mode,last_completed_account_id,last_native_id)"
                   " VALUES (?,?,?,?)",
                   ("historical", "pinned", "proxy", None))
    with pytest.raises(BridgeError) as error:
        store.admit("new", "historical", "hello", RunOptions(), None)
    assert error.value.code == "proxy_binding_unverified"


def test_old_proxy_binding_and_cached_observation_cannot_admit_without_login(tmp_path):
    store = Store(tmp_path / "state")
    account = Account("manual", "codex", name="Manual", provider="codex",
                      supported_models=(MODEL,), proxy_base_url="http://127.0.0.1:8317/v1",
                      key_env="CLIENT_KEY", management_key_env="MANAGEMENT_KEY")
    with store.connect() as db:
        db.execute("INSERT INTO accounts(id,config) VALUES (?,?)",
                   (account.id, json.dumps(account.to_dict())))
        db.execute("INSERT INTO proxy_bindings VALUES (?,?,?)",
                   (account.id, "a" * 64, "b" * 64))
    store.account_observation(account.id, "cliproxy_management", "active", {
        "account_id": account.id, "provider": account.provider,
        "status": "active", "disabled": False, "unavailable": False,
        "binding_verified": True, "models": [{"id": MODEL}],
    })
    with pytest.raises(BridgeError) as error:
        store.add_session("new", account.id, str(tmp_path), MODEL)
    assert error.value.code == "proxy_binding_unverified"
    assert store.list("sessions") == []


def test_transport_rejects_a_direct_account_before_argv_construction():
    account = SimpleNamespace(engine="codex", proxy_base_url=None)
    with pytest.raises(BridgeError) as error:
        command(account, {"model": MODEL}, RunOptions())
    assert error.value.code == "invalid_proxy_account"


@pytest.mark.parametrize("engine", ("codex", "claude"))
def test_worker_never_launches_a_historical_direct_account(tmp_path, monkeypatch, engine):
    store = Store(tmp_path / "state")
    _legacy_account(store, tmp_path, engine)
    _historical_session(store, tmp_path)
    now = time.time()
    with store.connect() as db:
        db.execute("INSERT INTO runs(id,message_id,session_id,account_id,state,prompt,options,created,updated) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   ("old-run", "old-run", "historical", "legacy", "starting",
                    "hello", json.dumps(asdict(RunOptions())), now, now))
    monkeypatch.setattr(worker, "Store", lambda _: store)
    monkeypatch.setattr(sys, "argv", ["worker", str(store.root), "old-run"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(worker.signal, "signal", lambda *_: None)

    def forbidden_launch(*_args, **_kwargs):
        raise AssertionError("A direct provider subprocess was launched.")

    monkeypatch.setattr(worker.subprocess, "Popen", forbidden_launch)
    with pytest.raises(SystemExit) as stopped:
        worker.main()
    assert stopped.value.code == 1
    run = store.get("runs", "old-run")
    assert run["state"] == "failed" and run["error"] == "invalid_proxy_account"
    assert run["child_pid"] is None


def test_management_key_reaches_verifier_but_not_codex_environment(tmp_path, monkeypatch):
    store = Store(tmp_path / "state")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    account = Account("proxy", "codex", provider="openai", supported_models=(MODEL,),
                      proxy_base_url="http://127.0.0.1:8317/v1", key_env="CLIENT_KEY",
                      management_key_env="MANAGEMENT_KEY")
    seed_authenticated_proxy_account(store, account, record_observation=False)
    now = time.time()
    with store.connect() as db:
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                   ("proxy-session", "proxy", str(workspace), MODEL, None, None, None, now))
        db.execute("INSERT INTO instance_metadata VALUES (?,?,?,?)",
                   ("proxy-session", "active", 1, now))
        db.execute("INSERT INTO session_routing"
                   "(session_id,mode,last_completed_account_id,last_native_id)"
                   " VALUES (?,?,?,?)",
                   ("proxy-session", "pinned", "proxy", None))
        db.execute("INSERT INTO runs(id,message_id,session_id,account_id,state,prompt,options,created,updated) "
                   "VALUES (?,?,?,?,?,?,?,?,?)",
                   ("proxy-run", "proxy-run", "proxy-session", "proxy", "starting",
                    "hello", json.dumps(asdict(RunOptions())), now, now))
    monkeypatch.delenv("MANAGEMENT_KEY", raising=False)
    monkeypatch.setattr(worker, "Store", lambda _: store)
    monkeypatch.setattr(sys, "argv", ["worker", str(store.root), "proxy-run"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "CLIENT_KEY": "private-client-secret", "MANAGEMENT_KEY": "private-management-secret"})))
    monkeypatch.setattr(worker.signal, "signal", lambda *_: None)
    observed = []

    def verify(_routes, _account, model, *, refresh):
        assert model == MODEL and refresh
        observed.append(("management", worker.os.environ.get("MANAGEMENT_KEY")))

    def launch(argv, *, env, **_kwargs):
        assert "private-management-secret" not in repr(argv)
        observed.append(("codex_env", dict(env)))
        raise OSError("private-launch-error")

    monkeypatch.setattr(worker, "verify_proxy_model", verify)
    monkeypatch.setattr(worker.ContractRegistry, "verify_run", lambda *_: None)
    monkeypatch.setattr(worker.subprocess, "Popen", launch)
    with pytest.raises(SystemExit):
        worker.main()
    assert observed[0] == ("management", "private-management-secret")
    assert observed[1][1]["CLIENT_KEY"] == "private-client-secret"
    assert "MANAGEMENT_KEY" not in observed[1][1]
    assert "MANAGEMENT_KEY" not in worker.os.environ
    evidence = json.dumps([event.data for event in store.events(run_id="proxy-run")])
    assert "private-management-secret" not in evidence
