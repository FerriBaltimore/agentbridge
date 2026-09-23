"""Usage and quota RPC/CLI surfaces report proxy evidence only."""

import json
import secrets
import time

import pytest

from agentbridge import Bridge
from agentbridge.cli import main
from agentbridge.errors import BridgeError
from agentbridge.rpc import dispatch
from test_accounts_service import configured_proxy, proxy_responses
from test_proxy_management import local_management


def test_quota_reset_is_unsupported_in_rpc_and_absent_from_cli(tmp_path, capsys):
    bridge = Bridge(tmp_path)
    with pytest.raises(BridgeError) as error:
        dispatch(bridge, "accounts.quota.reset", {
            "account_ref": "fixture", "idempotency_key": "key"})
    assert error.value.code == "unsupported"
    with pytest.raises(SystemExit) as exit_status:
        main(["--root", str(tmp_path), "accounts", "quota-reset", "fixture",
              "--idempotency-key", "key"])
    assert exit_status.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert bridge.capabilities()["operations"]["accounts.quota.reset"]["support"] == "unsupported"


def test_account_usage_methods_match_capability_discovery_and_rpc_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_MANAGEMENT_KEY", secrets.token_hex(16))
    with local_management(proxy_responses(used_percent="50")) as (port, _):
        bridge = Bridge(tmp_path)
        configured_proxy(bridge, port)
        operations = dispatch(bridge, "capabilities.get", {})["operations"]
        for method in ("accounts.usage", "accounts.usage_history", "usage.history"):
            assert operations[method]["support"] == "adapter"
            assert operations[method]["maturity"] == "fixture_tested"

        usage = dispatch(bridge, "accounts.usage", {
            "account_ref": "codex-test", "refresh": True})
        account_history = dispatch(bridge, "accounts.usage_history", {
            "account_ref": "codex-test"})
        usage_history = dispatch(bridge, "usage.history", {
            "account_ref": "codex-test"})

    assert usage["supported"] is True
    assert usage["quota_windows"][0]["used_percent"] == 50
    assert account_history[0]["id"] == usage_history[0]["id"]
    assert account_history[0]["observed_at"] == usage_history[0]["observed_at"]
    assert account_history[0]["data"]["quota_windows"][0]["used_percent"] == 50
    assert usage_history[0]["data"]["quota_windows"][0]["used_percent"] == 50


def test_usage_include_quota_keeps_live_proxy_observation(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_MANAGEMENT_KEY", secrets.token_hex(16))
    with local_management(proxy_responses(used_percent="50")) as (port, _):
        bridge = Bridge(tmp_path)
        configured_proxy(bridge, port)
        value = bridge.usage("account", account_ref="codex-test", include_quota=True,
                             refresh=True)
    assert value["source"] == "cliproxy_management"
    assert value["quota_windows"][0]["used_percent"] == 50
    assert value["supported"] is True and value["stale"] is False
    assert "quota" not in value


def test_proxy_history_retains_capture_age_and_marks_old_data_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_MANAGEMENT_KEY", secrets.token_hex(16))
    with local_management(proxy_responses(used_percent="20")) as (port, _):
        bridge = Bridge(tmp_path)
        configured_proxy(bridge, port)
        current = bridge.account_usage("codex-test", refresh=True)
        assert current["supported"] is True
    original = time.time() - 3600
    with bridge.store.connect() as db:
        db.execute("UPDATE usage_observations SET observed_at=? WHERE account_id=?",
                   (original, "codex-test"))
    history = bridge.account_usage_history("codex-test")
    assert len(history) == 1
    assert abs(history[0]["observed_at"] - original) < .01
    assert history[0]["stale"] and history[0]["data"]["stale"]
    assert history[0]["data"]["quota_windows"][0]["used_percent"] == 20
    assert history[0]["data"]["age_seconds"] >= 3600


def test_proxy_history_preserves_unknown_quota_instead_of_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_MANAGEMENT_KEY", secrets.token_hex(16))
    with local_management(proxy_responses(used_percent="unknown")) as (port, _):
        bridge = Bridge(tmp_path)
        configured_proxy(bridge, port)
        usage = bridge.account_usage("codex-test", refresh=True)
    history = bridge.account_usage_history("codex-test")
    assert usage["supported"] is False and usage["quota_windows"] == []
    assert history[0]["data"]["supported"] is False
    assert history[0]["data"]["quota_windows"] == []
    assert history[0]["data"]["stale"] is True
    assert history[0]["data"]["reason"] == "upstream_quota_unavailable"


def test_cli_error_keeps_safe_structured_metadata(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path), "accounts", "status", "missing"])
    value = json.loads(capsys.readouterr().err)
    assert value["data"]["code"] == "account_not_found"
    assert value["data"]["outcome"] == "not_started"
    assert value["data"]["retryable"] is False
