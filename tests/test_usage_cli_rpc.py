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


def test_reset_credit_rpc_and_cli_use_only_the_public_sdk(tmp_path, monkeypatch, capsys):
    seen = []

    def credits(self, account_ref=None, *, account_id=None, refresh=False):
        seen.append(("read", account_ref, account_id, refresh))
        return {"account_ref": account_ref, "available_count": 1,
                "status": "available", "stale": False,
                "observed_at": "2026-09-25T10:00:00Z", "observation_ref": "revision-1",
                "credits": [{"id": "credit-1", "status": "available"}]}

    def redeem(self, account_ref=None, *, account_id=None, idempotency_key,
               observation_ref, credit_id=None):
        seen.append(("redeem", account_ref, account_id, idempotency_key,
                     observation_ref, credit_id))
        return {"account_ref": account_ref, "outcome": "reset", "windows_reset": 1,
                "reset_credits": {"available_count": 0}}

    monkeypatch.setattr(Bridge, "account_reset_credits", credits)
    monkeypatch.setattr(Bridge, "account_quota_reset", redeem)
    bridge = Bridge(tmp_path)
    operations = bridge.capabilities()["operations"]
    for name in ("accounts.reset_credits", "accounts.quota.reset"):
        assert operations[name]["support"] == "adapter"
        assert operations[name]["maturity"] == "fixture_tested"
        assert "live_redemption_pending" in operations[name]["limitations"]
    assert "live_credit_read_one_account" in operations["accounts.reset_credits"]["limitations"]

    assert dispatch(bridge, "accounts.reset_credits", {
        "account_ref": "fixture", "refresh": True})["observation_ref"] == "revision-1"
    assert dispatch(bridge, "accounts.quota.reset", {
        "account_ref": "fixture", "idempotency_key": "logical-1",
        "observation_ref": "revision-1", "credit_id": "credit-1"})["outcome"] == "reset"

    main(["--root", str(tmp_path), "accounts", "reset-credits", "fixture",
          "--refresh", "--json"])
    assert json.loads(capsys.readouterr().out)["observation_ref"] == "revision-1"
    main(["--root", str(tmp_path), "accounts", "quota-reset", "fixture",
          "--idempotency-key", "logical-1", "--observation-ref", "revision-1",
          "--credit-id", "credit-1", "--json"])
    assert json.loads(capsys.readouterr().out)["outcome"] == "reset"
    assert seen == [
        ("read", "fixture", None, True),
        ("redeem", "fixture", None, "logical-1", "revision-1", "credit-1"),
        ("read", "fixture", None, True),
        ("redeem", "fixture", None, "logical-1", "revision-1", "credit-1"),
    ]


def test_quota_reset_cli_requires_explicit_key_and_observation(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(["--root", str(tmp_path), "accounts", "quota-reset", "fixture",
              "--idempotency-key", "logical-1"])
    assert error.value.code == 2
    assert "--observation-ref" in capsys.readouterr().err


def test_reset_credit_human_output_shows_reference_for_explicit_redemption(
        tmp_path, monkeypatch, capsys):
    def credits(self, account_ref=None, *, account_id=None, refresh=False):
        return {"account_ref": account_ref, "available_count": 1,
                "status": "available", "stale": False,
                "observed_at": "2026-09-25T10:00:00Z", "observation_ref": "revision-1",
                "credits": [{"id": "credit-1", "status": "available"}]}

    monkeypatch.setattr(Bridge, "account_reset_credits", credits)
    main(["--root", str(tmp_path), "accounts", "reset-credits", "fixture", "--refresh"])
    output = capsys.readouterr().out
    assert "Reset credits: 1" in output
    assert "Observation reference: revision-1" in output
    assert "Credit: credit-1 (available)" in output


def test_quota_reset_cli_preserves_unknown_outcome(tmp_path, monkeypatch, capsys):
    def unknown(self, account_ref=None, *, account_id=None, idempotency_key,
                observation_ref, credit_id=None):
        raise BridgeError("reset_outcome_unknown", "The Codex reset outcome is unknown.",
                          phase="redemption", outcome="unknown")

    monkeypatch.setattr(Bridge, "account_quota_reset", unknown)
    with pytest.raises(SystemExit) as error:
        main(["--root", str(tmp_path), "accounts", "quota-reset", "fixture",
              "--idempotency-key", "logical-1", "--observation-ref", "revision-1",
              "--json"])
    assert error.value.code == 1
    failure = json.loads(capsys.readouterr().err)
    assert failure["error"] == "reset_outcome_unknown"
    assert failure["data"]["outcome"] == "unknown"
    assert failure["data"]["retryable"] is False


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
