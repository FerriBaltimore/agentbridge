"""Provider reads that overlap a reset cannot publish pre-reset balances."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
import json

from agentbridge import Bridge
from agentbridge.proxy.management import ManagementClient
from agentbridge.proxy.reset_credits import CodexResetProxy
from test_account_resets import FIRST_KEY, bound_bridge, observed
from test_proxy_reset_credits import reset_proxy


def _complete_fixture_reset(bridge, snapshot):
    bridge.store.begin_reset_attempt(
        "fixture", FIRST_KEY, snapshot["observation_ref"], None,
        bridge.store.proxy_binding("fixture"))
    bridge.store.finish_reset_attempt(FIRST_KEY, "reset", 1)


def test_older_credit_read_cannot_publish_after_reset(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, _):
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        entered, release = Event(), Event()
        original = CodexResetProxy.read

        def paused_read(proxy):
            value = original(proxy)
            entered.set()
            assert release.wait(timeout=5)
            return value

        monkeypatch.setattr(CodexResetProxy, "read", paused_read)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(Bridge(bridge.root).account_reset_credits,
                                 "fixture", refresh=True)
            try:
                assert entered.wait(timeout=5)
                bridge.store.begin_reset_attempt(
                    "fixture", FIRST_KEY, snapshot["observation_ref"], None,
                    bridge.store.proxy_binding("fixture"))
                pending = bridge.account_reset_credits("fixture")
                assert pending["pending_reset"] is not None
                assert pending["status"] == "unknown"
                assert pending["available_count"] is None
                assert pending["observation_ref"] is None
                assert pending["stale"] is True
                bridge.store.finish_reset_attempt(FIRST_KEY, "reset", 1)
            finally:
                release.set()
            late = future.result(timeout=5)
        assert late["status"] == "unknown"
        assert late["available_count"] is None
        assert late["observation_ref"] is None
        assert late["stale"] is True
        assert bridge.account_reset_credits("fixture")["available_count"] is None


def test_older_quota_read_cannot_publish_late_timestamp(tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        state["entry"]["unavailable"] = False
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        state["usage"] = {"status_code": 200, "body": json.dumps({
            "rate_limit": {"primary_window": {
                "used_percent": 63, "limit_window_seconds": 18000}}})}
        entered, release = Event(), Event()
        original = ManagementClient._post_json

        def paused_reply(management, path, secret, payload):
            response = original(management, path, secret, payload)
            if path == "/v0/management/api-call":
                entered.set()
                assert release.wait(timeout=5)
            return response

        monkeypatch.setattr(ManagementClient, "_post_json", paused_reply)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(Bridge(bridge.root).account_usage,
                                 "fixture", refresh=True)
            try:
                assert entered.wait(timeout=5)
                _complete_fixture_reset(bridge, snapshot)
            finally:
                release.set()
            late = future.result(timeout=5)
        assert late["stale"] is True
        assert all(row["used_percent"] is None for row in late["quota_windows"])
        cached = bridge.account_usage("fixture")
        assert cached["stale"] is True
        assert all(row["used_percent"] is None for row in cached["quota_windows"])


def test_older_passive_observation_cannot_replace_post_reset_state(
        tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        state["entry"]["unavailable"] = False
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        before = bridge.store.latest_account_observation("fixture")["id"]
        state["entry"]["quota"] = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "signals": {"X-Codex-Primary-Used-Percent": "63"}}
        entered, release = Event(), Event()
        original = ManagementClient.observe

        def paused_observe(management):
            value = original(management)
            entered.set()
            assert release.wait(timeout=5)
            return value

        monkeypatch.setattr(ManagementClient, "observe", paused_observe)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(Bridge(bridge.root).routes.observe,
                                 bridge.account("fixture"))
            try:
                assert entered.wait(timeout=5)
                _complete_fixture_reset(bridge, snapshot)
            finally:
                release.set()
            assert future.result(timeout=5) is None
        assert bridge.store.latest_account_observation("fixture")["id"] == before


def test_post_reset_passive_cache_stays_unknown_until_upstream_read(
        tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        state["entry"]["unavailable"] = False
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        _complete_fixture_reset(bridge, snapshot)
        state["entry"]["quota"] = {
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "signals": {"X-Codex-Primary-Used-Percent": "63"}}
        assert bridge.routes.observe(bridge.account("fixture")) is not None
        passive = bridge.account_usage("fixture")
        assert passive["stale"] is True
        assert all(row["used_percent"] is None for row in passive["quota_windows"])
        assert bridge.routes.candidates("gpt-fixture")[0].quota == ()

        state["usage"] = {"status_code": 200, "body": json.dumps({
            "rate_limit": {"primary_window": {
                "used_percent": 12, "limit_window_seconds": 18000}}})}
        active = bridge.account_usage("fixture", refresh=True)
        assert active["stale"] is False
        assert any(row["used_percent"] == 12 for row in active["quota_windows"])
        state["entry"]["quota"]["observed_at"] = datetime.now(timezone.utc).isoformat()
        bridge.routes.observe(bridge.account("fixture"))
        later_passive = bridge.account_usage("fixture")
        assert later_passive["stale"] is False
        assert any(row["used_percent"] == 12 for row in later_passive["quota_windows"])


def test_older_credit_get_cannot_replace_newer_get_in_same_generation(
        tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        bridge = bound_bridge(tmp_path, client)
        entered, release = Event(), Event()
        original = CodexResetProxy.read

        def paused_first_read(proxy):
            result = original(proxy)
            if not entered.is_set():
                entered.set()
                assert release.wait(timeout=5)
            return result

        monkeypatch.setattr(CodexResetProxy, "read", paused_first_read)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(Bridge(bridge.root).account_reset_credits,
                                "fixture", refresh=True)
            try:
                assert entered.wait(timeout=5)
                state["usage"] = {"status_code": 200, "body": json.dumps({
                    "rate_limit_reset_credits": {"available_count": 0}})}
                state["details"] = {"status_code": 200, "body": json.dumps({
                    "available_count": 0, "credits": []})}
                newer = bridge.account_reset_credits("fixture", refresh=True)
                assert newer["available_count"] == 0
            finally:
                release.set()
            older = first.result(timeout=5)
        assert older["available_count"] == 0
        assert older["observation_ref"] == newer["observation_ref"]
        assert bridge.store.reset_observation("fixture")["data"]["available_count"] == 0


def test_older_quota_get_cannot_replace_newer_get_in_same_generation(
        tmp_path, monkeypatch):
    with reset_proxy(monkeypatch) as (client, _, state):
        state["entry"]["unavailable"] = False
        bridge = bound_bridge(tmp_path, client)
        snapshot = observed(bridge)
        _complete_fixture_reset(bridge, snapshot)
        state["usage"] = {"status_code": 200, "body": json.dumps({
            "rate_limit": {"primary_window": {
                "used_percent": 63, "limit_window_seconds": 18000}}})}
        entered, release = Event(), Event()
        original = ManagementClient.fetch_quota

        def paused_first_fetch(management, binding):
            result = original(management, binding)
            if not entered.is_set():
                entered.set()
                assert release.wait(timeout=5)
            return result

        monkeypatch.setattr(ManagementClient, "fetch_quota", paused_first_fetch)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(Bridge(bridge.root).account_usage,
                                "fixture", refresh=True)
            try:
                assert entered.wait(timeout=5)
                state["usage"] = {"status_code": 200, "body": json.dumps({
                    "rate_limit": {"primary_window": {
                        "used_percent": 12, "limit_window_seconds": 18000}}})}
                newer = bridge.account_usage("fixture", refresh=True)
                assert any(row["used_percent"] == 12 for row in newer["quota_windows"])
            finally:
                release.set()
            older = first.result(timeout=5)
        assert older["stale"] is False
        assert any(row["used_percent"] == 12 for row in older["quota_windows"])
        cached = bridge.account_usage("fixture")
        assert any(row["used_percent"] == 12 for row in cached["quota_windows"])
