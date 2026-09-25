"""Model-first CLI and JSON-RPC contract without provider accounts."""

import io
import json

import pytest

from agentbridge import Account, Bridge, cli
from agentbridge.commands.model_actions import print_models
from agentbridge.rpc import rpc
from fixtures.test_proxy_account_fixture import seed_authenticated_proxy_account


class RecordingBridge:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def models(self, engine=None, **options):
        self.calls.append(('models', engine, options))
        return {'engine': engine, 'source': 'configured', 'stale': False,
                'items': [{'id': 'provider/model', 'display_name': 'Model',
                           'providers': ['provider'],
                           'candidate_account_refs': ['Primary', 'Secondary'],
                           'observed_account_refs': ['Primary']}],
                'has_more': False}

    def instance_create(self, **options):
        self.calls.append(('create', options))
        return {'instance_id': 'fixture-instance', 'model': options['model'],
                'account_ref': options.get('account_ref') or 'Least Used',
                'replayed': False}

    def account_login(self, **options):
        self.calls.append(('login', options))
        return {'account': {'name': options['name'], 'provider': options['provider']},
                'identity': {}}

    def account_login_start(self, **options):
        self.calls.append(('login-start', options))
        return {'attempt_id': 'fixture-attempt', 'status': 'starting'}


def test_model_cli_uses_model_first_catalog_without_engine_filter(monkeypatch, capsys):
    bridge = RecordingBridge()
    monkeypatch.setattr(cli, 'Bridge', lambda _root: bridge)

    cli.main(['models', 'list'])
    output = capsys.readouterr().out
    assert 'provider/model | Model' in output
    assert 'Providers: provider' in output
    assert 'Observed accounts: Primary' in output
    assert 'Secondary' not in output
    assert 'Engine:' not in output
    assert bridge.calls[0] == ('models', None, {
        'account_ref': None, 'refresh': False, 'include_hidden': False,
        'include_deprecated': False, 'limit': None, 'cursor': 0})

    with pytest.raises(SystemExit):
        cli.main(['models', 'list', '--engine', 'codex'])
    assert 'unrecognized arguments: --engine codex' in capsys.readouterr().err


def test_model_cli_labels_unverified_account_declarations(capsys):
    print_models({'source': 'configured', 'stale': True,
                  'items': [{'id': 'provider/model',
                             'candidate_account_refs': ['Configured'],
                             'observed_account_refs': []}]})
    output = capsys.readouterr().out
    assert 'Configured accounts (unverified): Configured' in output
    assert 'Observed accounts:' not in output


def test_instance_cli_uses_model_and_auto_account_unless_pinned(monkeypatch, capsys):
    bridge = RecordingBridge()
    monkeypatch.setattr(cli, 'Bridge', lambda _root: bridge)

    cli.main(['instances', 'create', '--model', 'provider/model',
              '--workspace-path', '/workspace', '--idempotency-key', 'create-1'])
    output = capsys.readouterr().out
    assert 'Model: provider/model' in output
    assert 'Account: Least Used' in output
    assert 'Engine:' not in output
    assert bridge.calls[-1] == ('create', {'model': 'provider/model',
        'workspace_path': '/workspace', 'account_ref': None,
        'provider': None, 'routing_mode': None, 'idempotency_key': 'create-1'})

    cli.main(['instances', 'create', '--model', 'provider/model',
              '--account-ref', 'Pinned', '--json'])
    assert json.loads(capsys.readouterr().out)['account_ref'] == 'Pinned'
    assert bridge.calls[-1][1]['account_ref'] == 'Pinned'

    cli.main(['instances', 'create', '--model', 'provider/model',
              '--account-ref', 'Preferred', '--routing-mode', 'automatic', '--json'])
    capsys.readouterr()
    assert bridge.calls[-1][1]['account_ref'] == 'Preferred'
    assert bridge.calls[-1][1]['routing_mode'] == 'automatic'

    cli.main(['instances', 'create', '--model', 'provider/model',
              '--provider', 'claude', '--json'])
    capsys.readouterr()
    assert bridge.calls[-1][1]['provider'] == 'claude'


def test_v2_rpc_forwards_model_and_automatic_account_choice():
    bridge = RecordingBridge()
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'models.list', 'params': {}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'instances.create',
         'params': {'model': 'provider/model', 'workspace_path': '/workspace'}},
    ]
    output = io.StringIO()
    rpc(bridge, io.StringIO(''.join(json.dumps(item) + '\n' for item in requests)), output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]['result']['items'][0]['id'] == 'provider/model'
    assert responses[1]['result']['account_ref'] == 'Least Used'
    assert bridge.calls[0][0:2] == ('models', None)
    assert bridge.calls[1][1] == {'model': 'provider/model', 'workspace_path': '/workspace'}


def test_accounts_rpc_exposes_provider_and_models_without_proxy_configuration(tmp_path):
    with Bridge(tmp_path / 'state') as bridge:
        seed_authenticated_proxy_account(bridge.store, Account('fixture', 'codex', name='Personal',
            provider='fixture', supported_models=('fixture/model',),
            proxy_base_url='http://127.0.0.1:8317/v1', key_env='PRIVATE_KEY_REF',
            management_key_env='MANAGEMENT_KEY_REF'))
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'accounts.list', 'params': {}}
        output = io.StringIO()
        rpc(bridge, io.StringIO(json.dumps(request) + '\n'), output)

    account = json.loads(output.getvalue())['result'][0]
    assert account['provider'] == 'fixture'
    assert account['supported_models'] == ['fixture/model']
    assert 'proxy_base_url' not in account and 'key_env' not in account
    assert 'management_key_env' not in account


def test_proxy_account_login_cli_passes_route_and_provider_together(monkeypatch, capsys):
    bridge = RecordingBridge()
    monkeypatch.setattr(cli, 'Bridge', lambda _root: bridge)
    options = ['--name', 'Primary', '--provider', 'claude',
               '--proxy-base-url', 'http://127.0.0.1:8317/v1',
               '--proxy-key-env', 'PROXY_CLIENT_KEY_REF',
               '--proxy-management-key-env', 'PROXY_MANAGEMENT_KEY_REF']

    cli.main(['accounts', 'login', *options, '--json'])
    assert json.loads(capsys.readouterr().out)['account']['provider'] == 'claude'
    sent = bridge.calls[-1][1]
    assert {key: sent[key] for key in ('provider', 'name', 'proxy_base_url',
            'key_env', 'management_key_env')} == {
        'provider': 'claude', 'name': 'Primary',
        'proxy_base_url': 'http://127.0.0.1:8317/v1',
        'key_env': 'PROXY_CLIENT_KEY_REF',
        'management_key_env': 'PROXY_MANAGEMENT_KEY_REF'}
    assert 'engine' not in sent and 'inference' not in sent

    cli.main(['accounts', 'login-start', *options, '--json'])
    assert json.loads(capsys.readouterr().out)['attempt_id'] == 'fixture-attempt'
    sent = bridge.calls[-1][1]
    assert sent['provider'] == 'claude'
    assert sent['proxy_base_url'] == 'http://127.0.0.1:8317/v1'
    assert sent['key_env'] == 'PROXY_CLIENT_KEY_REF'
    assert sent['management_key_env'] == 'PROXY_MANAGEMENT_KEY_REF'
