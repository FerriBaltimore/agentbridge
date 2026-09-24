"""Proxy evidence and permission decisions remain explicit when data is missing."""

import os
from pathlib import Path
import secrets

import pytest

from agentbridge import Bridge
from agentbridge.errors import BridgeError
from agentbridge.permissions import Permissions
from agentbridge.models import RunOptions
from fixtures.test_proxy_account_fixture import register_verified_proxy_account
from test_accounts_service import configured_proxy, proxy_responses
from test_proxy_management import local_management


def test_failed_proxy_refresh_does_not_reuse_old_quota_as_current(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_MANAGEMENT_KEY", secrets.token_hex(16))
    responses = proxy_responses(used_percent="23")
    with local_management(responses) as (port, _):
        bridge = Bridge(tmp_path / "state")
        configured_proxy(bridge, port)
        first = bridge.account_usage("codex-test", refresh=True)
        assert first["supported"] is True and first["stale"] is False
        responses["/v0/management/auth-files"] = (503, {"error": "private body"}, {})
        value = bridge.account_usage("codex-test", refresh=True)

    assert value["supported"] is False and value["stale"] is True
    assert value["quota_windows"] == []
    assert value["reason"] == "proxy_observation_unavailable"
    observed = bridge.store.latest_account_observation("codex-test")
    assert observed["status"] == "unknown"
    assert observed["data"] == {"verified": False, "reason": "proxy_observation_unavailable"}
    assert "private body" not in repr(value) + repr(observed)
    assert bridge.runs() == []


def test_missing_proxy_quota_stays_unknown_instead_of_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_MANAGEMENT_KEY", secrets.token_hex(16))
    with local_management(proxy_responses(used_percent="not-a-number")) as (port, _):
        bridge = Bridge(tmp_path / "state")
        configured_proxy(bridge, port)
        value = bridge.account_usage("codex-test", refresh=True)

    assert value["supported"] is False
    assert value["stale"] is True
    assert value["quota_windows"] == []
    assert value["reason"] == "upstream_quota_unavailable"
    assert "used_percent" not in repr(value)


def admitted_turn(tmp_path):
    bridge = Bridge(tmp_path / "state")
    register_verified_proxy_account(bridge.store, "fixture", 8317)
    instance_id, created = bridge.store.add_session(
        "fixture-instance", "fixture", str(tmp_path), "fixture-model")
    assert created
    turn, admitted = bridge.store.admit(
        "fixture-turn", instance_id, "hello", RunOptions(), "request-key")
    assert admitted
    return bridge, turn


def test_expired_permission_is_denied_and_cannot_be_revived(tmp_path):
    bridge, turn = admitted_turn(tmp_path)
    permissions = Permissions(bridge.store)
    request = permissions.request(turn, {"operation": "fixture"}, timeout=0)
    assert permissions.wait(turn, request) == "deny"
    with pytest.raises(BridgeError) as error:
        permissions.respond(turn, request, "allow")
    assert error.value.code == "permission_expired"
    bridge.store.finish(turn, "cancelled")


def test_native_channel_timeout_is_bounded_for_an_incomplete_line(tmp_path):
    from agentbridge.provider_channel import ProviderChannel

    workspace = tmp_path / 'workspace'
    home = tmp_path / 'state/codex-runtime/instance'
    temporary = tmp_path / 'native-tmp'
    for path in (workspace, home, temporary):
        path.mkdir(parents=True)
    environment = {**os.environ, 'CODEX_HOME': str(home), 'HOME': str(home),
                   'TMPDIR': str(temporary),
                   'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    with ProviderChannel(
        ['/usr/bin/python3', "-c", 'import sys,time;sys.stdout.write("{");sys.stdout.flush();time.sleep(20)'],
        cwd=str(workspace), env=environment,
    ) as channel:
        with pytest.raises(BridgeError) as error:
            channel.receive(.1)
        assert error.value.code == "provider_timeout"
    assert channel.process.poll() is not None


def test_expired_queued_allow_reports_the_actual_delivered_denial(tmp_path):
    bridge, turn = admitted_turn(tmp_path)
    permissions = Permissions(bridge.store)
    request = permissions.request(turn, {"operation": "fixture"}, timeout=30)
    permissions.respond(turn, request, "allow")
    with bridge.store.connect() as db:
        db.execute("UPDATE permission_requests SET expires=0 WHERE id=?", (request,))
    applied = permissions.wait(turn, request)
    assert applied == "deny"
    permissions.delivered(turn, request, applied)
    replay = permissions.respond(turn, request, "allow")
    assert replay["state"] == "delivered" and replay["applied_decision"] == "deny"
    bridge.store.finish(turn, "cancelled")
