"""Local proxy routing uses references and cannot silently share endpoints."""

import json
import pytest

from agentbridge import Account, Bridge, RunOptions
from agentbridge.errors import BridgeError
from agentbridge.proxy import ProxyRoute, codex_overrides, validate_unique_endpoints
from agentbridge.transports import command


def test_codex_overrides_use_local_responses_endpoint_and_key_reference():
    route = ProxyRoute("account_a", "http://127.0.0.1:8317/v1", "ACCOUNT_A_PROXY_KEY")
    arguments = codex_overrides(route)
    assert 'model_provider="agentbridge_local_proxy"' in arguments
    assert 'model_providers.agentbridge_local_proxy.base_url="http://127.0.0.1:8317/v1"' in arguments
    assert 'model_providers.agentbridge_local_proxy.env_key="ACCOUNT_A_PROXY_KEY"' in arguments
    assert 'model_providers.agentbridge_local_proxy.wire_api="responses"' in arguments
    assert "-c" in arguments
    assert "secret-value" not in repr(route)


@pytest.mark.parametrize("endpoint", (
    "http://localhost:8317/v1", "http://192.168.1.2:8317/v1",
    "https://127.0.0.1:8317/v1", "http://127.0.0.1/v1",
    "http://127.0.0.1:0/v1", "http://127.0.0.1:8317/v1/extra",
    "http://user:password@127.0.0.1:8317/v1",
    "http://127.0.0.1:8317/v1?token=value",
    "http://127.0.0.1:8317/v1#fragment",
    "http://127.0.0.1:8317/v1\n",
))
def test_route_rejects_noncanonical_or_nonlocal_endpoints(endpoint):
    with pytest.raises(BridgeError) as error:
        ProxyRoute("account_a", endpoint, "ACCOUNT_A_PROXY_KEY")
    assert error.value.code == "invalid_proxy_endpoint"


def test_route_accepts_ipv6_loopback_and_rejects_credential_value():
    assert ProxyRoute("account_a", "http://[::1]:8317/v1", "ACCOUNT_A_PROXY_KEY").base_url
    with pytest.raises(BridgeError) as error:
        ProxyRoute("account_a", "http://127.0.0.1:8317/v1", "a key value")
    assert error.value.code == "invalid_environment"


def test_one_endpoint_cannot_be_assigned_to_two_accounts():
    first = ProxyRoute("account_a", "http://127.0.0.1:8317/v1", "A_KEY")
    second = ProxyRoute("account_b", "http://127.0.0.1:8317/v1", "B_KEY")
    with pytest.raises(BridgeError) as error:
        validate_unique_endpoints((first, second))
    assert error.value.code == "proxy_endpoint_shared"
    validate_unique_endpoints((first, first))


def test_codex_transport_keeps_route_overrides_for_new_resume_and_app_server():
    account = Account("account_a", "codex", provider="openai", supported_models=("gpt-5",),
                      proxy_base_url="http://127.0.0.1:8317/v1", key_env="ACCOUNT_A_PROXY_KEY")
    provider_option = 'model_provider="agentbridge_local_proxy"'
    for native_id in (None, "native-session"):
        argv = command(account, {"native_id": native_id, "model": "gpt-5"}, RunOptions())
        assert provider_option in argv
        assert '--model' in argv and argv[argv.index('--model') + 1] == "gpt-5"
        if native_id:
            assert argv[argv.index("resume") + 1] == native_id
    app_server = command(account, {"native_id": "native-session", "model": "gpt-5"},
                         RunOptions(permission_mode="default"), native_transport=True)
    assert app_server[1:3] == ["app-server", "--stdio"]
    assert provider_option in app_server


def test_context_window_override_uses_codex_config_for_both_transports():
    account = Account('account_a', 'codex', provider='codex',
                      supported_models=('fixture-model',),
                      proxy_base_url='http://127.0.0.1:8317/v1', key_env='CLIENT_KEY')
    session = {'native_id': None, 'model': 'fixture-model'}
    for native_transport, options in ((False, RunOptions(context_window=32768)),
                                      (True, RunOptions(context_window=32768,
                                                        permission_mode='default'))):
        argv = command(account, session, options, native_transport=native_transport)
        assert 'model_context_window=32768' in argv
    with pytest.raises(BridgeError) as error:
        command(account, session, RunOptions(context_window='large'))
    assert error.value.code == 'invalid_context_window'


@pytest.mark.parametrize("other_reference", ("key_env", "env_names"))
def test_management_key_reference_cannot_enter_codex_environment(other_reference):
    values = {"key_env": "CLIENT_KEY", "env_names": (), "management_key_env": "MANAGEMENT_KEY"}
    values[other_reference] = "MANAGEMENT_KEY" if other_reference == "key_env" else ("MANAGEMENT_KEY",)
    with pytest.raises(BridgeError) as error:
        Account("account_a", "codex", provider="codex", supported_models=("gpt-5",),
                proxy_base_url="http://127.0.0.1:8317/v1", **values)
    assert error.value.code == "invalid_proxy_account"


def test_pinned_proxy_turn_requires_a_verified_management_binding(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIENT_KEY", "local-fixture-key")
    bridge = Bridge(tmp_path.parent / f'{tmp_path.name}-state')
    account = Account("account_a", "codex", provider="codex",
        supported_models=("gpt-5",), proxy_base_url="http://127.0.0.1:8317/v1",
        key_env="CLIENT_KEY")
    with bridge.store.connect() as db:
        db.execute("INSERT INTO accounts(id,config) VALUES (?,?)",
                   (account.id, json.dumps(account.to_dict())))
    with pytest.raises(BridgeError) as error:
        bridge.session("account_a", tmp_path, model="gpt-5")
    assert error.value.code == "proxy_binding_unverified"
    assert bridge.sessions() == []
