"""Proxy-only admission keeps replay and portable evidence durable."""

import json

import pytest

from agentbridge import Bridge, RunOptions
from agentbridge.errors import BridgeError
from fixtures.test_proxy_account_fixture import register_verified_proxy_account


MODEL = "fixture-model"


def prepared(tmp_path):
    bridge = Bridge(tmp_path / "state")
    register_verified_proxy_account(bridge.store, "fixture", 8371, model=MODEL)
    bridge.store.add_session("instance", "fixture", str(tmp_path), MODEL)
    return bridge


def test_idempotent_admission_and_sql_events(tmp_path):
    bridge = prepared(tmp_path)
    options = RunOptions(model=MODEL)
    first, created = bridge.store.admit("run1", "instance", "hello", options, "same")
    second, replayed = bridge.store.admit("run2", "instance", "hello", options, "same")
    assert (first, created) == ("run1", True)
    assert (second, replayed) == ("run1", False)
    assert [event.kind for event in bridge.store.events(run_id="run1")] == ["user"]
    assert bridge.store.get("runs", "run1")["account_id"] == "fixture"


def test_context_marks_unknown_effect_and_keeps_archive(tmp_path):
    bridge = prepared(tmp_path)
    bridge.store.admit("run1", "instance", "do thing", RunOptions(model=MODEL), "key")
    bridge.store.emit("run1", "tool_call", {
        "call_id": "call", "name": "shell", "input": {"command": "touch x"},
    })

    bundle = bridge.export_context("instance", budget_bytes=10000)
    payload = json.loads(bundle.text.split("\n", 1)[1])
    assert payload["unknown_outcomes"][0]["call_id"] == "call"
    assert bundle.archive_sha256
    assert (bridge.root / "archives").is_dir()


def test_context_refuses_when_mandatory_evidence_does_not_fit(tmp_path):
    bridge = prepared(tmp_path)
    bridge.store.admit("run1", "instance", "x" * 5000, RunOptions(model=MODEL), "key")

    with pytest.raises(BridgeError) as caught:
        bridge.export_context("instance", budget_bytes=2048)
    assert caught.value.code == "context_over_budget"
