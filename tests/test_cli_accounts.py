import argparse
import io
import json
from types import SimpleNamespace

import pytest

from agentbridge import cli
from agentbridge.errors import BridgeError
from agentbridge.cli import main
from agentbridge.commands.parser import build_parser


class AccountBridge:
    def __init__(self):
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def account_login(self, **options):
        name = options['name']
        if any(row['name'].casefold() == name.casefold() for row in self.rows):
            raise BridgeError('account_name_in_use', 'Account name is already in use.')
        account = {'id': 'private-account-id', 'name': name,
                   'provider': options['provider'], 'email': options['email'],
                   'supported_models': []}
        self.rows.append(account)
        return {'account': account, 'identity': {}}

    def accounts(self):
        return list(self.rows)

    def resolve_account(self, reference):
        for row in self.rows:
            if row['name'].casefold() == reference.casefold():
                return SimpleNamespace(**row)
        raise BridgeError('account_not_found', 'No account matches that name.')

    def account_status(self, account_id, *, refresh=False):
        account = next(row for row in self.rows if row['id'] == account_id)
        return {'account_id': account_id, 'configured': account,
                'authentication': {'status': 'not_observed', 'observed_at': None},
                'identity': {}, 'reason': 'no_observation'}

    def account_delete(self, reference):
        account = self.resolve_account(reference)
        self.rows = [row for row in self.rows if row['id'] != account.id]
        return {'account_ref': account.name, 'removed': True,
                'upstream_credential_removed': False}


@pytest.fixture
def account_bridge(monkeypatch):
    bridge = AccountBridge()
    monkeypatch.setattr(cli, 'Bridge', lambda _root: bridge)
    return bridge


def login_options(name='Personal Codex'):
    return ['--provider', 'codex', '--name', name,
            '--proxy-base-url', 'http://127.0.0.1:8317/v1',
            '--proxy-key-env', 'PROXY_CLIENT_KEY_REF',
            '--proxy-management-key-env', 'PROXY_MANAGEMENT_KEY_REF']


def test_cli_without_arguments_prints_help(capsys):
    main([])
    output = capsys.readouterr().out
    assert 'usage: agentbridge' in output
    assert 'accounts' in output
    assert 'Examples:' in output
    assert '\033[' not in output


def test_accounts_without_command_prints_account_help(capsys):
    main(['accounts'])
    output = capsys.readouterr().out
    assert 'usage: agentbridge accounts' in output
    assert 'status' in output
    assert 'usage' in output
    assert 'login' in output


def test_help_uses_color_only_for_interactive_terminal(monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(cli.sys, 'stdout', Tty())
    monkeypatch.delenv('NO_COLOR', raising=False)
    parser = argparse.ArgumentParser(prog='agentbridge', formatter_class=cli.PrettyHelpFormatter)
    parser.add_argument('--example')
    assert '\033[36m' in parser.format_help()

    monkeypatch.setenv('NO_COLOR', '1')
    assert '\033[' not in parser.format_help()


def test_cli_login_and_list_accounts(account_bridge, capsys):
    main(['accounts', 'login', *login_options(), '--email', 'ferran@example.test'])
    assert 'Account authenticated: Personal Codex (codex)' in capsys.readouterr().out

    main(['accounts', 'list'])
    output = capsys.readouterr().out
    assert 'Personal Codex | codex | - | ferran@example.test' in output
    assert 'private-account-id' not in output


def test_cli_delete_explains_local_retirement_and_uses_sdk(account_bridge, capsys):
    main(['accounts', 'login', *login_options()])
    capsys.readouterr()
    main(['accounts', 'delete', 'Personal Codex'])
    output = capsys.readouterr().out
    assert 'Account removed from AgentBridge: Personal Codex' in output
    assert 'CLIProxyAPI OAuth credential remains' in output
    main(['accounts', 'list'])
    assert 'No accounts registered.' in capsys.readouterr().out


def test_cli_status_json_does_not_claim_unobserved_authentication(account_bridge, capsys):
    main(['accounts', 'login', *login_options('Codex Main')])
    capsys.readouterr()
    main(['accounts', 'status', 'Codex Main', '--json'])
    output = json.loads(capsys.readouterr().out)
    assert output['authentication']['status'] == 'not_observed'


def test_cli_account_name_is_unique_and_hides_internal_id(account_bridge, capsys):
    main(['accounts', 'login', *login_options('Test Account')])
    authenticated = capsys.readouterr().out
    assert 'Account authenticated: Test Account (codex)' in authenticated
    assert 'private-account-id' not in authenticated

    main(['accounts', 'list'])
    listing = capsys.readouterr().out
    assert listing == 'Name | Provider | Models | Email\n---|---|---|---\nTest Account | codex | - | -\n'

    main(['accounts', 'status', 'TEST ACCOUNT'])
    status = capsys.readouterr().out
    assert 'Account: Test Account' in status
    assert 'private-account-id' not in status

    with pytest.raises(SystemExit):
        main(['accounts', 'login', *login_options('test account')])
    assert 'account_name_in_use' in capsys.readouterr().err


@pytest.mark.parametrize('missing', ('--provider', '--name'))
@pytest.mark.parametrize('command', ('login', 'login-start'))
def test_login_requires_provider_and_name(command, missing, capsys):
    options = login_options()
    index = options.index(missing)
    del options[index:index + 2]
    with pytest.raises(SystemExit):
        main(['accounts', command, *options])
    assert 'required' in capsys.readouterr().err


@pytest.mark.parametrize('command', ('login', 'login-start'))
def test_login_uses_managed_proxy_by_default(command):
    parser, _ = build_parser()
    args = parser.parse_args(['accounts', command, '--provider', 'codex',
                              '--name', 'Personal'])
    assert args.proxy_base_url is None
    assert args.key_env is None
    assert args.management_key_env is None


def test_legacy_account_creation_and_engine_options_are_removed(capsys):
    with pytest.raises(SystemExit):
        main(['accounts', 'add', '--name', 'Old'])
    assert 'invalid choice' in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(['accounts', 'login', *login_options(), '--engine', 'codex'])
    assert 'unrecognized arguments: --engine codex' in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(['accounts', 'login-check', 'attempt', '--inference'])
    assert 'unrecognized arguments: --inference' in capsys.readouterr().err


@pytest.mark.parametrize('command', ('login', 'login-start'))
@pytest.mark.parametrize('option,value', (('--mode', 'device'), ('--mode', 'hosted'),
                                          ('--browser', 'mobile'), ('--browser', 'remote_desktop')))
def test_proxy_login_rejects_nonlocal_authentication_modes(command, option, value, capsys):
    parser, _ = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(['accounts', command, *login_options(), option, value])
    assert 'invalid choice' in capsys.readouterr().err


@pytest.mark.parametrize('command', ('login', 'login-start'))
def test_proxy_login_uses_local_browser_mode(command):
    parser, _ = build_parser()
    args = parser.parse_args(['accounts', command, *login_options()])
    assert (args.mode, args.browser) == ('browser', 'same_host')


def test_login_check_help_describes_proxy_verification(capsys):
    parser, _ = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(['accounts', '--help'])
    help_text = ' '.join(capsys.readouterr().out.split())
    assert 'Verify proxy identity and models through the local management API' in help_text
    assert 'fresh provider process' not in help_text
