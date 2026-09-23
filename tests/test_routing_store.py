"""Durable route selection preserves account identity and replay semantics."""

from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from agentbridge import Bridge
from agentbridge.errors import BridgeError
from agentbridge.models import Account, RunOptions
from agentbridge.routing import RouteDecision
from agentbridge.store import Store
from fixtures.test_proxy_account_fixture import proxy_account, register_verified_proxy_account


MODEL = "fixture-model"


def prepared(tmp_path):
    store = Store(tmp_path / "state")
    register_verified_proxy_account(store, "a", 8301, model=MODEL)
    register_verified_proxy_account(store, "b", 8302, model=MODEL)
    return store


def decision(account_id, *, used=20):
    return RouteDecision(account_id, MODEL, "known", used, "healthy", 0, "least_used")


def admit(store, run_id, session_id, account_id, *, key=None, context=None, omissions=0):
    return store.admit(run_id, session_id, "fixture prompt", RunOptions(model=MODEL), key,
                       account_id=account_id, route_decision=decision(account_id),
                       route_context=context, route_omissions=omissions,
                       route_event_seq=store.last_route_event_seq(session_id) if context else None)


def test_automatic_admission_persists_route_and_native_completion(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    assert store.routing("instance") == {"mode": "automatic",
                                          "last_completed_account_id": None,
                                          "last_native_id": None, "provider": None}
    # The first selected account may differ from the creation anchor.
    assert admit(store, "run-b", "instance", "b") == ("run-b", True)
    event = store.events(run_id="run-b")[0]
    assert event.kind == "route_selected"
    assert event.data["account_id"] == "b" and event.data["model"] == MODEL
    assert event.data["quota_state"] == "known" and event.data["reason"] == "least_used"
    assert event.data["account_changed"] is False
    assert event.data["portable_context_used"] is False
    assert store.get("runs", "run-b")["account_id"] == "b"
    store.emit("run-b", "session", {"native_id": "native-b"})
    store.finish("run-b", "completed")
    assert store.routing("instance")["last_completed_account_id"] == "b"
    assert store.routing("instance")["last_native_id"] == "native-b"


def test_switch_requires_bounded_context_and_failure_restores_stable_native(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    admit(store, "first", "instance", "a")
    store.emit("first", "session", {"native_id": "native-a"})
    store.finish("first", "completed")
    with pytest.raises(BridgeError) as caught:
        admit(store, "missing-context", "instance", "b")
    assert caught.value.code == "context_required"
    assert store.route_load(("b",))["b"]["assigned_turns"] == 0
    with pytest.raises(BridgeError) as caught:
        admit(store, "oversized", "instance", "b", context="x" * 128_001)
    assert caught.value.code == "context_over_budget"
    admit(store, "switch", "instance", "b", context="bounded history", omissions=3)
    session = store.get("sessions", "instance")
    assert session["account_id"] == "b" and session["native_id"] is None
    assert session["context"] == "bounded history"
    route_event = store.events(run_id="switch")[0]
    assert route_event.data["account_changed"] is True
    assert route_event.data["context_omitted_count"] == 3
    store.emit("switch", "session", {"native_id": "failed-native-b"})
    store.finish("switch", "failed", "provider_failed")
    session = store.get("sessions", "instance")
    assert (session["account_id"], session["native_id"]) == ("a", "native-a")
    assert store.routing("instance")["last_native_id"] == "native-a"
    store.finish("switch", "completed")  # Terminal replay cannot promote failed state.
    assert store.routing("instance")["last_completed_account_id"] == "a"
    admit(store, "next", "instance", "b", context="updated history")
    store.emit("next", "session", {"native_id": "native-b"})
    store.finish("next", "completed")
    assert store.routing("instance")["last_completed_account_id"] == "b"
    assert store.routing("instance")["last_native_id"] == "native-b"


def test_failed_first_run_requires_context_on_next_turn(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    admit(store, "failed", "instance", "a")
    store.emit("failed", "session", {"native_id": "unstable-native"})
    store.emit("failed", "tool_call", {"call_id": "fixture-call", "name": "shell"})
    store.finish("failed", "interrupted", "worker_lost")
    assert store.get("sessions", "instance")["native_id"] is None
    with pytest.raises(BridgeError) as caught:
        admit(store, "without-context", "instance", "a")
    assert caught.value.code == "context_required"
    admit(store, "continued", "instance", "a", context="Unknown prior tool outcome")
    event = store.events(run_id="continued")[0]
    assert event.data["portable_context_used"] is True
    assert event.data["account_changed"] is False
    assert store.get("sessions", "instance")["native_id"] is None


def test_automatic_resume_leaves_portable_context_to_turn_admission(tmp_path):
    bridge = Bridge(tmp_path / "state")
    register_verified_proxy_account(bridge.store, "a", 8301, model=MODEL)
    bridge.store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    admit(bridge.store, "failed", "instance", "a")
    bridge.store.emit("failed", "tool_call", {"call_id": "fixture-call", "name": "shell"})
    bridge.store.finish("failed", "failed", "provider_failed")
    seen = {}

    def capture_submit(session_id, prompt, **kwargs):
        seen.update({"session_id": session_id, "prompt": prompt})
        return prompt

    bridge.submit = capture_submit
    bridge.run("failed").resume()
    assert seen["session_id"] == "instance"
    assert "Historical conversation evidence" not in seen["prompt"]
    assert "Unknown previous outcomes" not in seen["prompt"]


def test_completed_without_native_requires_context_even_on_same_account(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    admit(store, "first", "instance", "a")
    store.finish("first", "completed")
    assert store.routing("instance")["last_native_id"] is None
    with pytest.raises(BridgeError) as caught:
        admit(store, "second", "instance", "a")
    assert caught.value.code == "context_required"
    admit(store, "second", "instance", "a", context="Previous completed answer")
    assert store.events(run_id="second")[0].data["portable_context_used"] is True


def test_failed_switch_requires_context_when_returning_to_stable_account(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    admit(store, "first", "instance", "a")
    store.emit("first", "session", {"native_id": "native-a"})
    store.finish("first", "completed")
    admit(store, "failed-b", "instance", "b", context="prior history")
    store.finish("failed-b", "failed", "provider_failed")
    with pytest.raises(BridgeError) as caught:
        admit(store, "return-a", "instance", "a")
    assert caught.value.code == "context_required"
    admit(store, "return-a", "instance", "a", context="History including failed B turn")
    event = store.events(run_id="return-a")[0]
    assert event.data["account_changed"] is True
    assert event.data["portable_context_used"] is True
    assert store.get("sessions", "instance")["native_id"] is None


def test_admission_rejects_context_built_before_new_evidence(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    admit(store, "first", "instance", "a")
    store.emit("first", "session", {"native_id": "native-a"})
    store.finish("first", "completed")
    snapshot = store.last_route_event_seq("instance")
    store.emit("first", "diagnostic", {"reason": "late_evidence"})
    with pytest.raises(BridgeError) as caught:
        store.admit("stale", "instance", "fixture prompt", RunOptions(model=MODEL), None,
                    account_id="b", route_decision=decision("b"),
                    route_context="history before the diagnostic", route_event_seq=snapshot)
    assert caught.value.code == "context_stale"
    assert store.route_load(("b",))["b"]["assigned_turns"] == 0


def test_replay_keeps_original_route_even_when_selection_changes(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    assert admit(store, "first", "instance", "a", key="same") == ("first", True)
    assert admit(store, "second", "instance", "b", key="same") == ("first", False)
    assert store.get("sessions", "instance")["account_id"] == "a"
    assert len([e for e in store.events(run_id="first") if e.kind == "route_selected"]) == 1
    assert store.route_load(("a", "b")) == {
        "a": {"in_flight": 1, "assigned_turns": 1},
        "b": {"in_flight": 0, "assigned_turns": 0},
    }


def test_automatic_create_replay_ignores_newly_selected_account(tmp_path):
    store = prepared(tmp_path)
    assert store.replay_auto_session("key", str(tmp_path), MODEL) is None
    assert store.add_session("first", "a", str(tmp_path), MODEL,
                             request_key="key", routing_mode="automatic") == ("first", True)
    assert store.replay_auto_session("key", str(tmp_path), MODEL) == "first"
    assert store.add_session("second", "b", str(tmp_path), MODEL,
                             request_key="key", routing_mode="automatic") == ("first", False)
    with pytest.raises(BridgeError) as caught:
        store.replay_auto_session("key", str(tmp_path), "different")
    assert caught.value.code == "idempotency_conflict"


def test_pinned_sessions_preserve_existing_admission_semantics(tmp_path):
    store = prepared(tmp_path)
    store.add_session("pinned", "a", str(tmp_path), MODEL)
    assert store.routing("pinned")["mode"] == "pinned"
    with pytest.raises(BridgeError) as caught:
        admit(store, "wrong", "pinned", "b")
    assert caught.value.code == "invalid_request"
    store.admit("pinned-run", "pinned", "fixture prompt", RunOptions(), None)
    assert [event.kind for event in store.events(run_id="pinned-run")] == ["user"]


def test_v3_migration_preserves_pinned_proxy_session_and_replay(tmp_path):
    store = prepared(tmp_path)
    store.add_session("legacy", "a", str(tmp_path), MODEL,
                      native_id="native-a", request_key="legacy-key")
    with store.connect() as db:
        db.execute("DROP TABLE session_routing")
        db.execute("UPDATE metadata SET version=3")
    upgraded = Store(tmp_path / "state")
    with upgraded.connect() as db:
        assert db.execute("SELECT version FROM metadata").fetchone()[0] == 6
    assert upgraded.routing("legacy") == {"mode": "pinned",
                                          "last_completed_account_id": "a",
                                          "last_native_id": "native-a", "provider": None}
    assert upgraded.add_session("ignored", "a", str(tmp_path), MODEL,
                                native_id="native-a", request_key="legacy-key") == ("legacy", False)


def test_legacy_account_cannot_start_a_new_session_after_v2_upgrade(tmp_path):
    store = prepared(tmp_path)
    native = Account("native", "codex", home=str(tmp_path / "native-home"))
    with store.connect() as db:
        db.execute("INSERT INTO accounts(id,config) VALUES (?,?)",
                   (native.id, json.dumps(native.to_dict())))
    with pytest.raises(BridgeError) as caught:
        store.add_session("legacy-session", "native", str(tmp_path), MODEL)
    assert caught.value.code == "invalid_proxy_account"
    assert store.list("sessions") == []


def test_portable_context_and_replay_keep_the_selected_proxy_account(tmp_path):
    bridge = Bridge(tmp_path / "state")
    register_verified_proxy_account(bridge.store, "a", 8301, model=MODEL)
    register_verified_proxy_account(bridge.store, "b", 8302, model=MODEL)
    bridge.store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    admit(bridge.store, "first", "instance", "a")
    bridge.store.emit("first", "session", {"native_id": "native-a"})
    bridge.store.finish("first", "completed")
    bundle = bridge.export_context("instance")
    assert bridge.store.admit("switch", "instance", "fixture prompt", RunOptions(model=MODEL),
                              "switch-key", account_id="b", route_decision=decision("b"),
                              route_context=bundle.text, route_omissions=len(bundle.omitted),
                              route_event_seq=bridge.store.last_route_event_seq("instance")) == ("switch", True)
    assert bridge.store.routing("instance")["mode"] == "automatic"
    assert bridge.store.get("sessions", "instance")["account_id"] == "b"
    assert bridge.store.admit("retry", "instance", "fixture prompt", RunOptions(model=MODEL),
                              "switch-key", account_id="a", route_decision=decision("a")) == ("switch", False)
    assert bridge.store.get("sessions", "instance")["account_id"] == "b"


def test_one_account_cannot_admit_two_active_turns_concurrently(tmp_path):
    store = prepared(tmp_path)
    for session_id in ("one", "two"):
        store.add_session(session_id, "a", str(tmp_path), MODEL, routing_mode="automatic")

    def submit(index):
        try:
            return admit(store, f"run-{index}", ("one", "two")[index], "a")
        except BridgeError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, (0, 1)))
    assert sorted("admitted" if isinstance(item, tuple) else item for item in outcomes) == ["admitted", "busy"]
    assert store.route_load(("a",))["a"] == {"in_flight": 1, "assigned_turns": 1}


def test_direct_store_registration_is_disabled_without_exposing_url_or_key(tmp_path):
    store = prepared(tmp_path)
    with pytest.raises(BridgeError) as caught:
        store.account(proxy_account("c", 8301, model=MODEL))
    assert caught.value.code == "authentication_required"
    assert "8301" not in str(caught.value) and "FIXTURE_PROXY_KEY" not in str(caught.value)


def test_route_event_discards_unrecognized_decision_fields(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    raw = {**decision("a").__dict__, "token": "private-fixture-value"}
    store.admit("run", "instance", "fixture prompt", RunOptions(model=MODEL), None,
                account_id="a", route_decision=raw)
    with store.connect() as db:
        data = db.execute("SELECT data FROM events WHERE run_id='run' AND kind='route_selected'").fetchone()[0]
    assert "private-fixture-value" not in data
    assert json.loads(data)["account_id"] == "a"


def test_rejects_inconsistent_route_evidence_before_admission(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    inconsistent = {**decision("a").__dict__, "quota_state": "unknown"}
    with pytest.raises(BridgeError) as caught:
        store.admit("run", "instance", "fixture prompt", RunOptions(model=MODEL), None,
                    account_id="a", route_decision=inconsistent)
    assert caught.value.code == "invalid_request"
    assert store.route_load(("a",))["a"]["assigned_turns"] == 0
