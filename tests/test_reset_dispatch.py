"""Dispatch leases and canonical receipts for concurrent Codex reset requests."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import json
import time

import pytest

from agentbridge import Bridge
from agentbridge.errors import BridgeError
from agentbridge.proxy.reset_credits import CodexResetProxy
from agentbridge.store import Store
from test_account_resets import FIRST_KEY, bound_bridge, observed, provider_calls
from test_proxy_reset_credits import reset_proxy


def test_one_dispatch_per_key_until_first_request_returns(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        entered, release = Event(), Event()
        original = CodexResetProxy.consume

        def paused_consume(proxy, key, credit_id=None):
            entered.set()
            assert release.wait(timeout=5)
            return original(proxy, key, credit_id)

        monkeypatch.setattr(CodexResetProxy, "consume", paused_consume)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                bridge.account_quota_reset, "fixture", idempotency_key=FIRST_KEY,
                observation_ref=snapshot["observation_ref"])
            assert entered.wait(timeout=5)
            with pytest.raises(BridgeError) as blocked:
                Bridge(bridge.root).account_quota_reset(
                    "fixture", idempotency_key=FIRST_KEY,
                    observation_ref=snapshot["observation_ref"])
            assert blocked.value.code == "reset_in_progress"
            assert blocked.value.outcome == "unknown"
            release.set()
            assert first.result(timeout=5)["outcome"] == "reset"
        assert len(provider_calls(state, "POST")) == 1


def test_sdk_returns_first_durable_receipt_if_later_provider_reply_differs(
        tmp_path, monkeypatch):
    response = {"status_code": 200, "body": json.dumps({"code": "already_redeemed"})}
    with reset_proxy(monkeypatch, consume_response=response) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        original = bridge.store.finish_reset_attempt

        def concurrent_finish(key, outcome, windows_reset, **kwargs):
            original(key, "reset", 1)
            return original(key, outcome, windows_reset, **kwargs)

        monkeypatch.setattr(bridge.store, "finish_reset_attempt", concurrent_finish)
        result = bridge.account_quota_reset(
            "fixture", idempotency_key=FIRST_KEY,
            observation_ref=snapshot["observation_ref"])
        assert result["outcome"] == "reset"
        assert result["windows_reset"] == 1
        assert len(provider_calls(state, "POST")) == 1


def test_expired_dispatch_claim_allows_explicit_same_key_recovery(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        bridge.store.begin_reset_attempt(
            "fixture", FIRST_KEY, snapshot["observation_ref"], None,
            bridge.store.proxy_binding("fixture"))
        with bridge.store.connect() as db:
            db.execute("UPDATE account_reset_attempts SET dispatch_started=? "
                       "WHERE idempotency_key=?", (time.time() - 91, FIRST_KEY))
        result = Bridge(bridge.root).account_quota_reset(
            "fixture", idempotency_key=FIRST_KEY,
            observation_ref=snapshot["observation_ref"])
        assert result["outcome"] == "reset"
        assert len(provider_calls(state, "POST")) == 1


def test_cached_credit_identity_change_is_not_presented_as_available(
        tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        assert snapshot["status"] == "available" and not snapshot["stale"]
        with bridge.store.connect() as db:
            db.execute("UPDATE proxy_bindings SET binding_fingerprint=? WHERE account_id=?",
                       ("a" * 64, "fixture"))
        cached = bridge.account_reset_credits("fixture")
        assert cached["status"] == "unknown"
        assert cached["available_count"] is None
        assert cached["observation_ref"] is None
        assert cached["stale"] is True
        assert cached["reason"] == "proxy_binding_changed"
        assert provider_calls(state, "POST") == []


def test_known_redemption_survives_subsequent_refresh_failures(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)

        def unavailable(*_args, **_kwargs):
            raise OSError("synthetic local refresh failure")

        monkeypatch.setattr(bridge, "account_reset_credits", unavailable)
        monkeypatch.setattr(bridge, "account_usage", unavailable)
        result = bridge.account_quota_reset(
            "fixture", idempotency_key=FIRST_KEY,
            observation_ref=snapshot["observation_ref"])
        assert result["outcome"] == "reset"
        assert result["reset_credits"]["status"] == "unknown"
        assert result["reset_credits"]["stale"] is True
        assert result["usage_refresh_reason"] == "usage_refresh_unavailable"
        assert len(provider_calls(state, "POST")) == 1


def test_version_eight_store_upgrades_to_reset_tables(tmp_path):
    root = tmp_path / "state"
    store = Store(root)
    with store.connect() as db:
        db.execute("DROP TABLE account_reset_observations")
        db.execute("DROP TABLE account_reset_attempts")
        db.execute("DROP TABLE account_reset_generations")
        db.execute("ALTER TABLE session_routing DROP COLUMN affinity_account_id")
        db.execute("UPDATE metadata SET version=8")
    upgraded = Store(root)
    with upgraded.connect() as db:
        assert db.execute("SELECT version FROM metadata").fetchone()[0] == 13
        assert db.execute("PRAGMA table_info(account_reset_attempts)").fetchall()
    assert upgraded.pending_reset_account_ids() == set()


def test_version_nine_store_without_reset_tables_upgrades(tmp_path):
    root = tmp_path / "state"
    store = Store(root)
    with store.connect() as db:
        db.execute("DROP TABLE account_reset_observations")
        db.execute("DROP TABLE account_reset_attempts")
        db.execute("DROP TABLE account_reset_generations")
        db.execute("DROP TABLE queued_messages")
        db.execute("DROP TABLE conversation_queues")
        db.execute("ALTER TABLE session_routing DROP COLUMN affinity_account_id")
        db.execute("UPDATE metadata SET version=9")
    upgraded = Store(root)
    with upgraded.connect() as db:
        assert db.execute("SELECT version FROM metadata").fetchone()[0] == 13
        assert db.execute("PRAGMA table_info(account_reset_attempts)").fetchall()
        assert db.execute("PRAGMA table_info(queued_messages)").fetchall()


def test_version_twelve_store_without_reset_tables_upgrades(tmp_path):
    root = tmp_path / "state"
    store = Store(root)
    with store.connect() as db:
        db.execute("DROP TABLE account_reset_observations")
        db.execute("DROP TABLE account_reset_attempts")
        db.execute("DROP TABLE account_reset_generations")
        db.execute("UPDATE metadata SET version=12")
    upgraded = Store(root)
    with upgraded.connect() as db:
        assert db.execute("SELECT version FROM metadata").fetchone()[0] == 13
        assert db.execute("PRAGMA table_info(native_session_bindings)").fetchall()
        assert db.execute("PRAGMA table_info(account_reset_observations)").fetchall()
        assert db.execute("PRAGMA table_info(account_reset_generations)").fetchall()
