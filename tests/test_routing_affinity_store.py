"""A selected automatic route has durable ownership independent of native state."""

import time

import pytest

from agentbridge import Bridge
from agentbridge.errors import BridgeError
from agentbridge.models import RunOptions
from agentbridge.routing import RouteDecision
from agentbridge.store import Store
from fixtures.test_proxy_account_fixture import register_verified_proxy_account


MODEL = "fixture-model"


def prepared(tmp_path):
    store = Store(tmp_path / "state")
    register_verified_proxy_account(store, "a", 8301, model=MODEL)
    register_verified_proxy_account(store, "b", 8302, model=MODEL)
    return store


def admit(store, run_id, account_id, *, reason=None, context=None, key=None):
    state, used = ("unknown", None) if account_id == "b" else ("known", 20)
    reason = reason or ("quota_unknown" if account_id == "b" else "affinity")
    decision = RouteDecision(account_id, MODEL, state, used, "healthy", 0, reason)
    return store.admit(
        run_id, "instance", "fixture prompt", RunOptions(model=MODEL), key,
        account_id=account_id, route_decision=decision, route_context=context,
        route_event_seq=store.last_route_event_seq("instance") if context else None,
    )


def test_automatic_creation_persists_affinity_and_explicit_initial_choice(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL,
                      request_key="create", routing_mode="automatic",
                      initial_account_id="a")
    route = Store(tmp_path / "state").routing("instance")
    assert route["affinity_account_id"] == "a"
    assert route["last_completed_account_id"] is None
    assert route["last_native_id"] is None
    assert store.replay_auto_session("create", str(tmp_path), MODEL,
                                     initial_account_id="a") == "instance"
    for choice in (None, "b"):
        with pytest.raises(BridgeError) as caught:
            store.replay_auto_session("create", str(tmp_path), MODEL,
                                      initial_account_id=choice)
        assert caught.value.code == "idempotency_conflict"


def test_failed_new_route_keeps_affinity_and_restores_last_good_native(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    assert admit(store, "first", "a") == ("first", True)
    store.emit("first", "session", {"native_id": "native-a"})
    store.finish("first", "completed")

    assert admit(store, "first-b", "b", context="bounded history") == (
        "first-b", True)
    assert store.routing("instance")["affinity_account_id"] == "b"
    store.emit("first-b", "session", {"native_id": "unstable-native-b"})
    store.finish("first-b", "failed", "provider_failed")

    reopened = Store(tmp_path / "state")
    route = reopened.routing("instance")
    assert route["affinity_account_id"] == "b"
    assert route["last_completed_account_id"] == "a"
    assert route["last_native_id"] == "native-a"
    session = reopened.get("sessions", "instance")
    assert (session["account_id"], session["native_id"]) == ("a", "native-a")
    assert admit(reopened, "retry-b", "b", reason="affinity",
                 context="history including failed B") == (
        "retry-b", True)
    assert reopened.events(run_id="retry-b")[0].data["reason"] == "affinity"


def test_rejected_or_replayed_admission_cannot_change_affinity(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    assert admit(store, "first", "a", key="turn-key") == ("first", True)
    assert admit(store, "duplicate", "b", key="turn-key") == ("first", False)
    assert store.routing("instance")["affinity_account_id"] == "a"
    store.finish("first", "failed", "provider_failed")
    with pytest.raises(BridgeError) as caught:
        admit(store, "false-affinity", "b", reason="affinity", context="prior history")
    assert caught.value.code == "invalid_request"
    with pytest.raises(BridgeError) as caught:
        admit(store, "invalid", "b", reason="least_used", context="prior history")
    assert caught.value.code == "invalid_request"
    assert store.routing("instance")["affinity_account_id"] == "a"
    assert store.route_load(("b",))["b"]["assigned_turns"] == 0


def test_v10_migration_recovers_last_admitted_route_after_failure(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    store.add_session("untouched", "b", str(tmp_path), MODEL, routing_mode="automatic")
    store.add_session("pinned", "a", str(tmp_path), MODEL)
    admit(store, "first", "a")
    store.emit("first", "session", {"native_id": "native-a"})
    store.finish("first", "completed")
    admit(store, "failed-b", "b", context="prior history")
    store.finish("failed-b", "failed", "provider_failed")
    with store.connect() as db:
        db.executescript('''
            CREATE TABLE session_routing_v10 AS
                SELECT session_id,mode,last_completed_account_id,last_native_id,provider
                  FROM session_routing;
            DROP TABLE session_routing;
            ALTER TABLE session_routing_v10 RENAME TO session_routing;
            UPDATE metadata SET version=10;
        ''')

    upgraded = Store(tmp_path / "state")
    assert upgraded.routing("instance")["affinity_account_id"] == "b"
    assert upgraded.routing("instance")["last_completed_account_id"] == "a"
    assert upgraded.routing("untouched")["affinity_account_id"] == "b"
    assert upgraded.routing("pinned")["affinity_account_id"] is None
    with upgraded.connect() as db:
        assert db.execute("SELECT version FROM metadata").fetchone()[0] == 12


def test_quota_break_evidence_identifies_previous_affinity(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    evidence = {"account_id": "a", "source": "fixture", "window_id": "five-hour",
                "observed_at": time.time(), "reset_at": None,
                "used_percent": 100, "limit_reached": True}
    decision = {"account_id": "b", "model": MODEL, "quota_state": "unknown",
                "used_percent": None, "reason": "quota_unknown",
                "affinity_break_reason": "quota_exhausted",
                "affinity_break_evidence": evidence}
    store.admit("switched", "instance", "fixture prompt", RunOptions(model=MODEL), None,
                account_id="b", route_decision=decision)
    event = store.events(run_id="switched")[0]
    assert event.data["affinity_break_reason"] == "quota_exhausted"
    assert event.data["affinity_break_evidence"] == evidence
    assert store.routing("instance")["affinity_account_id"] == "b"


def test_pinned_change_requires_snapshot_and_clears_other_account_native(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL)
    options = RunOptions(model=MODEL)
    store.admit("first", "instance", "first prompt", options, None, account_id="a")
    store.emit("first", "session", {"native_id": "native-a"})
    store.finish("first", "completed")
    with store.connect() as db:
        db.execute("UPDATE sessions SET account_id='b',native_id=NULL WHERE id='instance'")

    with pytest.raises(BridgeError) as caught:
        store.admit("missing", "instance", "next prompt", options, None, account_id="b")
    assert caught.value.code == "context_required"
    with pytest.raises(BridgeError) as caught:
        store.admit("stale", "instance", "next prompt", options, None, account_id="b",
                    route_context="bounded history", route_event_seq=0)
    assert caught.value.code == "context_stale"
    with pytest.raises(BridgeError) as caught:
        store.admit("wrong-account", "instance", "next prompt", options, None,
                    account_id="a", route_context="bounded history",
                    route_event_seq=store.last_route_event_seq("instance"))
    assert caught.value.code == "invalid_request"

    store.admit("switched", "instance", "next prompt", options, None, account_id="b",
                route_context="bounded history", route_event_seq=store.last_route_event_seq("instance"))
    session = store.get("sessions", "instance")
    assert (session["account_id"], session["native_id"], session["context"]) == (
        "b", None, "bounded history")
    assert store.routing("instance")["last_completed_account_id"] == "a"
    assert store.routing("instance")["last_native_id"] == "native-a"
    assert store.routing("instance")["affinity_account_id"] is None
    assert all(event.kind != "route_selected" for event in store.events(run_id="switched"))


def test_pinned_failed_switch_does_not_resume_unstable_native(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL)
    options = RunOptions(model=MODEL)
    store.admit("first", "instance", "first prompt", options, None, account_id="a")
    store.emit("first", "session", {"native_id": "native-a"})
    store.finish("first", "completed")
    with store.connect() as db:
        db.execute("UPDATE sessions SET account_id='b',native_id=NULL WHERE id='instance'")
    store.admit("failed-b", "instance", "next prompt", options, None, account_id="b",
                route_context="bounded history", route_event_seq=store.last_route_event_seq("instance"))
    store.emit("failed-b", "session", {"native_id": "unstable-native-b"})
    store.finish("failed-b", "failed", "provider_failed")
    with pytest.raises(BridgeError) as caught:
        store.admit("unsafe", "instance", "third prompt", options, None, account_id="b")
    assert caught.value.code == "context_required"
    store.admit("safe", "instance", "third prompt", options, None, account_id="b",
                route_context="history including failed B",
                route_event_seq=store.last_route_event_seq("instance"))
    assert store.get("sessions", "instance")["native_id"] is None


@pytest.mark.parametrize("mode", ("pinned", "automatic"))
def test_manual_a_b_a_without_b_turn_restores_owned_native(tmp_path, mode):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode=mode)
    options = RunOptions(model=MODEL)
    if mode == "automatic":
        admit(store, "first", "a")
    else:
        store.admit("first", "instance", "first prompt", options, None, account_id="a")
    store.emit("first", "session", {"native_id": "native-a"})
    store.finish("first", "completed")

    store.update_session("instance", routing_mode=mode, routing_account_id="b")
    assert store.get("sessions", "instance")["native_id"] is None
    store.update_session("instance", routing_mode=mode, routing_account_id="a")
    session = store.get("sessions", "instance")
    assert (session["account_id"], session["native_id"]) == ("a", "native-a")
    assert store.routing("instance")["last_native_id"] == "native-a"
    if mode == "automatic":
        admit(store, "next", "a")
        assert store.events(run_id="next")[0].data["portable_context_used"] is False
    else:
        store.admit("next", "instance", "next prompt", options, None, account_id="a")


def test_auto_admission_requires_context_if_native_was_cleared(tmp_path):
    store = prepared(tmp_path)
    store.add_session("instance", "a", str(tmp_path), MODEL, routing_mode="automatic")
    admit(store, "first", "a")
    store.emit("first", "session", {"native_id": "native-a"})
    store.finish("first", "completed")
    store.update_session("instance", routing_mode="pinned", routing_account_id="b")
    store.update_session("instance", routing_mode="automatic")
    assert store.get("sessions", "instance")["native_id"] is None
    with pytest.raises(BridgeError) as caught:
        admit(store, "unsafe", "a", reason="least_used")
    assert caught.value.code == "context_required"
    admit(store, "safe", "a", reason="least_used", context="bounded history")
    assert store.get("sessions", "instance")["native_id"] is None


@pytest.mark.parametrize("mode", ("pinned", "automatic"))
def test_explicit_create_replays_after_account_retirement(tmp_path, monkeypatch, mode):
    bridge = Bridge(tmp_path / "state")
    register_verified_proxy_account(bridge.store, "a", 8301, model=MODEL)
    monkeypatch.setattr(bridge.routes, "observation", lambda account, **_:
                        bridge.store.latest_account_observation(account.id))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = {"routing_mode": mode, "account_ref": "id:a", "model": MODEL,
               "workspace_path": str(workspace), "idempotency_key": "create-once"}
    created = bridge.instance_create(**request)
    bridge.store.retire_account("a")
    replayed = bridge.instance_create(**request)
    assert replayed["id"] == created["id"]
    assert replayed["replayed"] is True
    with pytest.raises(BridgeError) as caught:
        bridge.instance_create(**{**request, "idempotency_key": "new-request"})
    assert caught.value.code == "account_removed"
