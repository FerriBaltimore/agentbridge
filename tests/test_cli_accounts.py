import argparse
import io
import pytest

from agentbridge import cli
from agentbridge.cli import main


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


def test_cli_add_and_list_accounts(tmp_path, capsys):
    home = tmp_path / 'codex-home'
    home.mkdir()
    root = tmp_path / 'state'
    main(['--root', str(root), 'accounts', 'add', '--engine', 'codex',
          '--home', str(home), '--name', 'Personal Codex', '--email', 'ferran@example.test'])
    assert 'Account added: Personal Codex (codex)' in capsys.readouterr().out

    main(['--root', str(root), 'accounts', 'list'])
    output = capsys.readouterr().out
    assert 'Personal Codex | codex | ferran@example.test' in output


def test_cli_status_json_does_not_claim_unobserved_authentication(tmp_path, capsys):
    home = tmp_path / 'codex-home'
    home.mkdir()
    root = tmp_path / 'state'
    main(['--root', str(root), 'accounts', 'add', '--engine', 'codex', '--home', str(home), '--name', 'Codex Main'])
    capsys.readouterr()
    main(['--root', str(root), 'accounts', 'status', 'Codex Main', '--json'])
    output = capsys.readouterr().out
    assert '"status": "not_observed"' in output


def test_cli_account_name_is_unique_and_hides_internal_id(tmp_path, capsys):
    home = tmp_path / 'codex-home'
    home.mkdir()
    root = tmp_path / 'state'
    main(['--root', str(root), 'accounts', 'add', '--engine', 'codex', '--home', str(home), '--name', 'Test Account'])
    added = capsys.readouterr().out
    assert added == 'Account added: Test Account (codex)\n'

    main(['--root', str(root), 'accounts', 'list'])
    listing = capsys.readouterr().out
    assert listing == 'Name | Engine | Email\n---|---|---\nTest Account | codex | -\n'

    main(['--root', str(root), 'accounts', 'status', 'TEST ACCOUNT'])
    status = capsys.readouterr().out
    assert 'Account: Test Account' in status
    assert 'account_id' not in status

    with pytest.raises(SystemExit):
        main(['--root', str(root), 'accounts', 'add', '--engine', 'codex', '--home', str(home), '--name', 'test account'])
    assert 'account_name_in_use' in capsys.readouterr().err
