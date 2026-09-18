import json
import textwrap

from agentbridge import Account, Bridge


def fake_app_server(path):
    path.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import json, os, sys
        for line in sys.stdin:
            request = json.loads(line)
            method = request.get("method")
            if "id" not in request:
                continue
            if method == "initialize":
                result = {"serverInfo": {"name": "fake-codex"}}
            elif method == "account/read":
                result = {"account": {"type": "chatgpt", "email": "ferran@example.test", "planType": "pro"}, "requiresOpenaiAuth": False}
            elif method == "account/rateLimits/read":
                result = {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1900000000}}}
            elif method == "account/usage/read":
                result = {"summary": {"lifetimeTokens": 1234}, "dailyUsageBuckets": []}
            else:
                result = {}
            print(json.dumps({"id": request["id"], "result": result}), flush=True)
    '''))
    path.chmod(0o700)


def test_account_status_and_usage_are_observed_without_a_model_run(tmp_path, monkeypatch):
    provider = tmp_path / 'fake-codex'
    fake_app_server(provider)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('FAKE_PROVIDER_SECRET', 'secret-value')
    bridge = Bridge(tmp_path / 'state')
    bridge.register(Account('codex-test', 'codex', home=str(home), command=(str(provider),),
                            env_names=('FAKE_PROVIDER_SECRET',), name='Codex test'))

    status = bridge.account_status('codex-test', refresh=True)
    assert status['authentication']['status'] == 'authenticated'
    assert status['identity']['email'] == 'ferran@example.test'
    assert status['configured']['name'] == 'Codex test'

    usage = bridge.account_usage('codex-test', refresh=True)
    assert usage['source'] == 'codex_app_server'
    assert usage['quota']['rateLimits']['primary']['usedPercent'] == 12
    assert usage['account_usage']['summary']['lifetimeTokens'] == 1234

    stored = bridge.store.latest_account_observation('codex-test')
    assert 'FAKE_PROVIDER_SECRET' not in json.dumps(stored)


def test_account_service_does_not_call_unsupported_providers(tmp_path):
    bridge = Bridge(tmp_path / 'state')
    bridge.register(Account('cursor-test', 'cursor', command=('cursor',), name='Cursor test'))
    status = bridge.account_status('cursor-test', refresh=True)
    assert status['authentication']['status'] == 'unsupported'
    assert bridge.account_usage('cursor-test')['supported'] is False


def test_account_status_reports_no_observation_without_claiming_authentication(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    bridge = Bridge(tmp_path / 'state')
    bridge.register(Account('codex-test', 'codex', home=str(home)))
    status = bridge.account_status('codex-test')
    assert status['authentication']['status'] == 'not_observed'
    assert status['reason'] == 'no_observation'
