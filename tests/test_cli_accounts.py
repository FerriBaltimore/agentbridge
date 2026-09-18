from agentbridge.cli import main


def test_cli_add_and_list_accounts(tmp_path, capsys):
    home = tmp_path / 'codex-home'
    home.mkdir()
    root = tmp_path / 'state'
    main(['--root', str(root), 'accounts', 'add', 'codex-main', '--engine', 'codex',
          '--home', str(home), '--name', 'Personal Codex', '--email', 'ferran@example.test'])
    assert 'Cuenta añadida: codex-main (codex)' in capsys.readouterr().out

    main(['--root', str(root), 'accounts', 'list'])
    output = capsys.readouterr().out
    assert 'codex-main | codex | Personal Codex | ferran@example.test' in output


def test_cli_status_json_does_not_claim_unobserved_authentication(tmp_path, capsys):
    home = tmp_path / 'codex-home'
    home.mkdir()
    root = tmp_path / 'state'
    main(['--root', str(root), 'accounts', 'add', 'codex-main', '--engine', 'codex', '--home', str(home)])
    capsys.readouterr()
    main(['--root', str(root), 'accounts', 'status', 'codex-main', '--json'])
    output = capsys.readouterr().out
    assert '"status": "not_observed"' in output
