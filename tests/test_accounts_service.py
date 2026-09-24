"""Public account reads use the one verified local proxy route."""

import json
import secrets
from datetime import datetime, timezone

import pytest

from agentbridge import Account, Bridge
from agentbridge.errors import BridgeError
from test_proxy_management import EMPTY_CONFIG, local_management
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account


def proxy_responses(*, used_percent="12"):
    observed_at = datetime.now(timezone.utc).isoformat()
    return {
        "/v0/management/auth-files": (200, {
            "files": [{"name": "one.json", "source": "file", "runtime_only": False,
                       "provider": "codex", "status": "active", "disabled": False,
                       "unavailable": False, "auth_index": "private-index",
                       "account_type": "oauth",
                       "id_token": {"chatgpt_account_id": "private-account"},
                       "email": "private@example.test", "cooldowns": [],
                       "quota": {"observed_at": observed_at,
                                 "signals": {"X-Codex-Primary-Used-Percent": used_percent}}}],
        }, {}),
        "/v0/management/config": (200, EMPTY_CONFIG, {}),
        "/v0/management/auth-files/models?name=one.json": (
            200, {"models": [{"id": "gpt-test", "display_name": "Private provider metadata"}]}, {}),
    }


def configured_proxy(bridge, port, *, local=True):
    """Seed completed login evidence; public creation is covered by OAuth tests."""
    account = Account("codex-test", "codex", name="Codex test", provider="codex",
                      supported_models=("gpt-test",),
                      proxy_base_url=f"http://127.0.0.1:{port}/v1",
                      key_env="FIXTURE_PROXY_KEY",
                      management_key_env="FIXTURE_MANAGEMENT_KEY")
    return seed_authenticated_proxy_account(bridge.store, account,
        observe_local=local, record_observation=False)


def test_account_status_and_usage_are_observed_without_a_model_run(tmp_path, monkeypatch):
    management_key = secrets.token_hex(16)
    monkeypatch.setenv("FIXTURE_MANAGEMENT_KEY", management_key)
    responses = proxy_responses()
    with local_management(responses) as (port, seen):
        bridge = Bridge(tmp_path / "state")
        configured_proxy(bridge, port)

        status = bridge.account_status("codex-test", refresh=True)
        assert status["authentication"]["status"] == "active"
        assert status["authentication"]["source"] == "cliproxy_management"
        assert status["binding_verified"] is True
        assert status["configured"]["name"] == "Codex test"

        usage = bridge.account_usage("codex-test")
        assert usage["source"] == "cliproxy_management"
        assert usage["supported"] is True and usage["stale"] is False
        assert len(usage["quota_windows"]) == 1
        assert usage["quota_windows"][0]["scope"] == "account"
        assert usage["quota_windows"][0]["model_id"] is None
        assert usage["quota_windows"][0]["used_percent"] == 12.0
        assert usage["quota_windows"][0]["observed_at"] == (
            responses["/v0/management/auth-files"][1]["files"][0]["quota"]["observed_at"]
            .replace("+00:00", "Z"))
        assert bridge.runs() == []
        assert all(header == f"Bearer {management_key}" for _, header in seen)
        stored = bridge.store.latest_account_observation("codex-test")
        assert "private-index" not in json.dumps(stored)
        assert "private-account" not in json.dumps(stored)
        assert "private@example.test" not in json.dumps(stored)
        assert management_key not in json.dumps(stored)


def test_model_catalog_reports_proxy_evidence_without_a_model_run(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_MANAGEMENT_KEY", secrets.token_hex(16))
    with local_management(proxy_responses()) as (port, _):
        bridge = Bridge(tmp_path / "state")
        configured_proxy(bridge, port)
        catalog = bridge.models(account_ref="Codex test", refresh=True)

    assert catalog["source"] == "agentbridge_routing"
    assert catalog["models"][0]["id"] == "gpt-test"
    assert catalog["models"][0]["providers"] == ["codex"]
    assert catalog["models"][0]["availability"] == "proxy_observed"
    assert catalog["models"][0]["observed_account_refs"] == ["Codex test"]
    assert bridge.runs() == []


def test_public_registration_cannot_create_a_second_account_flow(tmp_path):
    bridge = Bridge(tmp_path / "state")
    account = Account("old", "codex", home=str(tmp_path / "native"))
    with pytest.raises(BridgeError) as error:
        bridge.register(account)
    assert error.value.code == "authentication_required"
    assert bridge.accounts() == []


def test_unobserved_proxy_does_not_claim_authentication_or_zero_quota(tmp_path):
    bridge = Bridge(tmp_path / "state")
    configured_proxy(bridge, 8317, local=False)
    status = bridge.account_status("codex-test")
    assert status["authentication"]["status"] == "not_observed"
    assert status["reason"] == "no_observation"
    assert bridge.store.latest_account_observation("codex-test") is None
