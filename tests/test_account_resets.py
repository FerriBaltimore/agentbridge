"""Earned Codex resets stay account-bound, explicit, and durable in the v2 SDK."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import time

import pytest

from agentbridge import Account, Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.proxy.reset_credits import CodexResetProxy
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account
from test_proxy_reset_credits import reset_proxy


CREDIT_ID = "RateLimitResetCredit_fixture"
FIRST_KEY = "fixture-reset-first"
SECOND_KEY = "fixture-reset-second"


def bound_bridge(tmp_path, client):
    bridge = Bridge(tmp_path / "state")
    account = Account(
        "fixture", "codex", name="Fixture", provider="codex",
        supported_models=("gpt-fixture",),
        proxy_base_url=client.route.base_url,
        key_env="FIXTURE_RESET_CLIENT_KEY",
        management_key_env="FIXTURE_RESET_MANAGEMENT_KEY",
    )
    seed_authenticated_proxy_account(bridge.store, account, observe_local=True)
    return bridge


def provider_calls(state, method):
    return [call for call in state["calls"] if call["method"] == method]


def observed(bridge):
    value = bridge.account_reset_credits("fixture", refresh=True)
    assert value["account_id"] == "fixture"
    return value


def test_read_is_cached_and_persists_only_normalized_credit_facts(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        empty = bridge.account_reset_credits("fixture")
        assert empty["status"] == "unknown"
        assert empty["available_count"] is None
        assert empty["stale"] is True
        assert empty["reason"] == "not_observed"
        assert state["calls"] == []

        fresh = observed(bridge)
        assert fresh["status"] == "available"
        assert fresh["available_count"] == 2
        assert fresh["stale"] is False
        assert len(fresh["credits"]) == 1
        assert fresh["credits"][0]["id"] == CREDIT_ID
        assert fresh["observation_ref"]
        assert fresh["pending_reset"] is None
        assert bridge.account_reset_credits("fixture")["observation_ref"] == (
            fresh["observation_ref"])
        assert len(provider_calls(state, "GET")) == 2
        assert Bridge(bridge.root).account_reset_credits("fixture")["observation_ref"] == (
            fresh["observation_ref"])

        stored = bridge.store.reset_observation("fixture")
        assert stored["data"]["available_count"] == 2
        database = (bridge.root / "bridge.sqlite3").read_bytes()
        assert b"provider-private-field" not in database
        assert b"opaque-fixture-index" not in database
        assert b"acct-fixture" not in database
        assert b"Bearer $TOKEN$" not in database


@pytest.mark.parametrize("count,expected_status", [(0, "none"), (2, "available")])
def test_count_distinguishes_none_from_available(tmp_path, monkeypatch, count, expected_status):
    with reset_proxy(monkeypatch, count=count) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        assert snapshot["available_count"] == count
        assert snapshot["status"] == expected_status
        if count == 0:
            with pytest.raises(BridgeError) as error:
                bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                           observation_ref=snapshot["observation_ref"])
            assert error.value.code == "reset_credit_unavailable"
            assert provider_calls(state, "POST") == []
            assert bridge.store.pending_reset_attempt("fixture") is None


def test_unavailable_read_is_unknown_and_cannot_authorize_redemption(tmp_path, monkeypatch):
    unavailable = {"status_code": 401, "body": "private provider error"}
    with reset_proxy(monkeypatch, details=unavailable) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        state["usage"] = unavailable
        view = observed(bridge)
        assert view["available_count"] is None
        assert view["status"] == "unknown"
        assert view["stale"] is True
        assert view["reason"] == "upstream_reset_unavailable"
        assert view["observation_ref"] is None
        with pytest.raises(BridgeError) as error:
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref="missing-observation")
        assert error.value.code == "reset_observation_changed"
        assert provider_calls(state, "POST") == []
        assert "private provider error" not in repr(view)


def test_failed_refresh_expires_prior_available_observation(tmp_path, monkeypatch):
    unavailable = {"status_code": 503, "body": "private upstream error"}
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        first = observed(bridge)
        assert first["available_count"] == 2
        state["usage"] = unavailable
        state["details"] = unavailable
        failed = bridge.account_reset_credits("fixture", refresh=True)
        assert failed["stale"] is True
        assert failed["reason"] == "upstream_reset_unavailable"
        assert failed["available_count"] == 2
        assert failed["observed_at"] is None
        with pytest.raises(BridgeError) as error:
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref=first["observation_ref"])
        assert error.value.code == "reset_observation_stale"
        assert provider_calls(state, "POST") == []


def test_redemption_persists_pending_before_post_and_replays_known_result(
        tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        original = CodexResetProxy.consume

        def inspect_pending(proxy, key, credit_id=None):
            attempt = Bridge(bridge.root).store.pending_reset_attempt("fixture")
            assert attempt is not None
            assert attempt["idempotency_key"] == key
            assert attempt["observation_ref"] == snapshot["observation_ref"]
            assert attempt["credit_id"] == CREDIT_ID
            return original(proxy, key, credit_id)

        monkeypatch.setattr(CodexResetProxy, "consume", inspect_pending)
        result = bridge.account_quota_reset(
            "fixture", idempotency_key=FIRST_KEY,
            observation_ref=snapshot["observation_ref"], credit_id=CREDIT_ID)
        assert result["outcome"] == "reset"
        assert result["windows_reset"] == 1
        assert result["reset_credits"]["observation_ref"] != snapshot["observation_ref"]
        assert bridge.store.pending_reset_attempt("fixture") is None
        assert bridge.store.begin_reset_attempt(
            "fixture", FIRST_KEY, snapshot["observation_ref"], CREDIT_ID,
            bridge.store.proxy_binding("fixture"))["state"] == "done"
        assert len(provider_calls(state, "POST")) == 1

        replay = Bridge(bridge.root).account_quota_reset(
            "fixture", idempotency_key=FIRST_KEY,
            observation_ref=snapshot["observation_ref"], credit_id=CREDIT_ID)
        assert replay["outcome"] == "reset"
        assert replay["windows_reset"] == 1
        assert len(provider_calls(state, "POST")) == 1


def test_unknown_outcome_keeps_exact_key_and_blocks_other_attempts(tmp_path, monkeypatch):
    response = {"status_code": 503, "body": "private provider error"}
    with reset_proxy(monkeypatch, consume_response=response) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        with pytest.raises(BridgeError) as error:
            bridge.account_quota_reset(
                "fixture", idempotency_key=FIRST_KEY,
                observation_ref=snapshot["observation_ref"], credit_id=CREDIT_ID)
        assert error.value.code == "reset_outcome_unknown"
        assert error.value.outcome == "unknown"
        pending = Bridge(bridge.root).store.pending_reset_attempt("fixture")
        assert pending["idempotency_key"] == FIRST_KEY
        assert pending["state"] == "pending"
        assert bridge.account_reset_credits("fixture")["pending_reset"]["idempotency_key"] == FIRST_KEY
        with pytest.raises(BridgeError) as other:
            bridge.account_quota_reset(
                "fixture", idempotency_key=SECOND_KEY,
                observation_ref=snapshot["observation_ref"], credit_id=CREDIT_ID)
        assert other.value.code == "reset_pending"
        assert len(provider_calls(state, "POST")) == 1

        state["consume"] = {"status_code": 200,
                            "body": json.dumps({"code": "already_redeemed"})}
        retry = Bridge(bridge.root).account_quota_reset(
            "fixture", idempotency_key=FIRST_KEY,
            observation_ref=snapshot["observation_ref"], credit_id=CREDIT_ID)
        assert retry["outcome"] == "already_redeemed"
        assert retry["windows_reset"] is None
        assert bridge.store.pending_reset_attempt("fixture") is None
        assert len(provider_calls(state, "POST")) == 2


def test_unknown_attempt_survives_later_live_binding_change(tmp_path, monkeypatch):
    response = {"status_code": 503, "body": "private provider error"}
    with reset_proxy(monkeypatch, consume_response=response) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        with pytest.raises(BridgeError) as first:
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref=snapshot["observation_ref"])
        assert first.value.outcome == "unknown"
        assert len(provider_calls(state, "POST")) == 1
        state["entry"]["id_token"] = {"chatgpt_account_id": "replacement-account"}
        with pytest.raises(BridgeError) as second:
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref=snapshot["observation_ref"])
        assert second.value.code == "reset_outcome_unknown"
        assert second.value.outcome == "unknown"
        assert bridge.store.pending_reset_attempt("fixture")["idempotency_key"] == FIRST_KEY
        assert len(provider_calls(state, "POST")) == 1


def test_failed_receipt_persistence_reports_unknown_and_keeps_pending(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)

        def fail_finish(*_args):
            raise OSError("synthetic SQLite write failure")

        monkeypatch.setattr(bridge.store, "finish_reset_attempt", fail_finish)
        with pytest.raises(BridgeError) as error:
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref=snapshot["observation_ref"])
        assert error.value.code == "reset_outcome_unknown"
        assert error.value.outcome == "unknown"
        assert "synthetic SQLite" not in str(error.value)
        assert bridge.store.pending_reset_attempt("fixture")["state"] == "pending"
        assert len(provider_calls(state, "POST")) == 1


def test_pending_redemption_blocks_routing_until_known_outcome(tmp_path, monkeypatch):
    response = {"status_code": 503, "body": "private provider error"}
    with reset_proxy(monkeypatch, consume_response=response) as (client, _, state):
        state["entry"]["unavailable"] = False
        bridge = bound_bridge(tmp_path, client)
        assert [item.account_id for item in bridge.routes.candidates("gpt-fixture")] == [
            "fixture"]
        snapshot = observed(bridge)
        with pytest.raises(BridgeError) as unknown:
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref=snapshot["observation_ref"])
        assert unknown.value.outcome == "unknown"
        assert bridge.routes.candidates("gpt-fixture") == []
        assert bridge.models(account_ref="fixture")["models"] == []
        with pytest.raises(BridgeError) as blocked:
            bridge.store.add_session("blocked", "fixture", str(tmp_path), "gpt-fixture")
        assert blocked.value.code == "reset_pending"

        state["consume"] = {"status_code": 200,
                            "body": json.dumps({"code": "nothing_to_reset"})}
        assert bridge.account_quota_reset(
            "fixture", idempotency_key=FIRST_KEY,
            observation_ref=snapshot["observation_ref"])["outcome"] == "nothing_to_reset"
        assert [item.account_id for item in bridge.routes.candidates("gpt-fixture")] == [
            "fixture"]


def test_pending_redemption_prevents_account_retirement(tmp_path, monkeypatch):
    response = {"status_code": 503, "body": "private provider error"}
    with reset_proxy(monkeypatch, consume_response=response) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        with pytest.raises(BridgeError) as unknown:
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref=snapshot["observation_ref"])
        assert unknown.value.code == "reset_outcome_unknown"
        with pytest.raises(BridgeError) as blocked:
            bridge.account_delete("fixture")
        assert blocked.value.code == "reset_pending"
        assert bridge.store.retirement_status("fixture")["retired"] is False
        assert bridge.store.proxy_binding("fixture") is not None
        state["consume"] = {"status_code": 200,
                            "body": json.dumps({"code": "already_redeemed"})}
        assert bridge.account_quota_reset(
            "fixture", idempotency_key=FIRST_KEY,
            observation_ref=snapshot["observation_ref"])["outcome"] == "already_redeemed"


def test_pending_redemption_prevents_account_reauthentication(tmp_path, monkeypatch):
    response = {"status_code": 503, "body": "private provider error"}
    with reset_proxy(monkeypatch, consume_response=response) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        with pytest.raises(BridgeError):
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref=snapshot["observation_ref"])
        attempt = {"id": "new-login", "owner": "fixture-owner", "account_id": "fixture",
                   "engine": "codex", "name": "Fixture", "mode": "browser",
                   "browser": "same_host", "status": "starting", "data": {}}
        with pytest.raises(BridgeError) as blocked:
            bridge.store.create_auth_attempt(attempt)
        assert blocked.value.code == "reset_pending"
        assert len(provider_calls(state, "POST")) == 1


def test_known_reset_invalidates_earlier_quota_percentages(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        state["entry"]["unavailable"] = False
        state["entry"]["quota"] = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "signals": {"X-Codex-Primary-Used-Percent": "63"}}
        bridge = bound_bridge(tmp_path, client)
        before = bridge.account_usage("fixture")
        assert any(row["used_percent"] == 63 for row in before["quota_windows"])
        snapshot = observed(bridge)
        result = bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                            observation_ref=snapshot["observation_ref"])
        assert result["outcome"] == "reset"
        assert bridge.store.reset_invalidation_at("fixture") is not None
        after = bridge.account_usage("fixture")
        assert after["quota_windows"]
        assert all(row["used_percent"] is None for row in after["quota_windows"])
        assert after["stale"] is True
        assert after["reason"] == "reset_quota_refresh_required"


def test_observation_revision_credit_and_idempotency_are_fenced(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        first = observed(bridge)
        second = observed(bridge)
        assert first["observation_ref"] != second["observation_ref"]
        with pytest.raises(BridgeError) as old:
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref=first["observation_ref"])
        assert old.value.code == "reset_observation_changed"
        with pytest.raises(BridgeError) as credit:
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref=second["observation_ref"],
                                       credit_id="wrong-credit")
        assert credit.value.code == "reset_credit_changed"
        assert provider_calls(state, "POST") == []

        bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                   observation_ref=second["observation_ref"],
                                   credit_id=CREDIT_ID)
        with pytest.raises(BridgeError) as conflict:
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref=second["observation_ref"])
        assert conflict.value.code == "idempotency_conflict"
        with pytest.raises(BridgeError) as used:
            bridge.account_quota_reset("fixture", idempotency_key=SECOND_KEY,
                                       observation_ref=second["observation_ref"],
                                       credit_id=CREDIT_ID)
        assert used.value.code == "reset_observation_changed"
        assert len(provider_calls(state, "POST")) == 1


def test_expired_observation_rejected_before_provider_mutation(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        with bridge.store.connect() as db:
            db.execute("UPDATE account_reset_observations SET observed_at=? WHERE account_id=?",
                       (time.time() - 61, "fixture"))
        assert bridge.account_reset_credits("fixture")["stale"] is True
        with pytest.raises(BridgeError) as error:
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref=snapshot["observation_ref"])
        assert error.value.code == "reset_observation_stale"
        assert provider_calls(state, "POST") == []


def test_active_run_prevents_redemption_before_provider_mutation(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        state["entry"]["unavailable"] = False
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        bridge.store.add_session("instance", "fixture", str(tmp_path), "gpt-fixture")
        bridge.store.admit("active", "instance", "hello", RunOptions(), None)
        with pytest.raises(BridgeError) as error:
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref=snapshot["observation_ref"])
        assert error.value.code == "busy"
        assert bridge.store.pending_reset_attempt("fixture") is None
        assert provider_calls(state, "POST") == []


def test_changed_stored_binding_prevents_redemption(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        with bridge.store.connect() as db:
            db.execute("UPDATE proxy_bindings SET binding_fingerprint=? WHERE account_id=?",
                       ("a" * 64, "fixture"))
        with pytest.raises(BridgeError) as error:
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref=snapshot["observation_ref"])
        assert error.value.code == "proxy_binding_changed"
        assert bridge.store.pending_reset_attempt("fixture") is None
        assert provider_calls(state, "POST") == []


def test_changed_live_binding_is_not_left_as_pending_attempt(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        state["entry"]["id_token"] = {"chatgpt_account_id": "replacement-account"}
        with pytest.raises(BridgeError) as error:
            bridge.account_quota_reset("fixture", idempotency_key=FIRST_KEY,
                                       observation_ref=snapshot["observation_ref"])
        assert error.value.code == "proxy_binding_changed"
        assert error.value.outcome == "not_started"
        assert bridge.store.pending_reset_attempt("fixture") is None
        assert provider_calls(state, "POST") == []


def test_reset_requires_grantbridge_bound_codex_account(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = Bridge(tmp_path / "state")
        codex = Account("fixture", "codex", name="Fixture", provider="codex",
                        supported_models=("gpt-fixture",),
                        proxy_base_url=client.route.base_url,
                        key_env="FIXTURE_RESET_CLIENT_KEY",
                        management_key_env="FIXTURE_RESET_MANAGEMENT_KEY")
        with bridge.store.connect() as db:
            db.execute("INSERT INTO accounts(id,config) VALUES (?,?)",
                       (codex.id, json.dumps(codex.to_dict())))
        with pytest.raises(BridgeError) as unverified:
            bridge.account_reset_credits("fixture", refresh=True)
        assert unverified.value.code == "proxy_binding_unverified"
        assert state["calls"] == []

        claude = replace(codex, id="claude", provider="claude", name="Claude")
        seed_authenticated_proxy_account(bridge.store, claude)
        with pytest.raises(BridgeError) as unsupported:
            bridge.account_reset_credits("claude", refresh=True)
        assert unsupported.value.code == "unsupported"
        assert state["calls"] == []
