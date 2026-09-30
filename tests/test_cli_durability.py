"""Administrative launch configuration cannot silently enable legacy native history."""

import json

import pytest

from agentbridge import Bridge
from agentbridge.cli import main
from fixtures.test_checkpoint_fixture import execution, prepared


def test_required_cli_initializes_fresh_owner_and_legacy_remains_explicit(tmp_path, capsys):
    root = tmp_path / 'fresh'
    main(['--root', str(root), '--owner-ref', 'trusted-owner', '--durability', 'required',
          'capabilities'])
    value = json.loads(capsys.readouterr().out)['durability']
    assert value['enabled'] and value['owner_ref'] == 'trusted-owner'
    assert value['host_upgrade_version'] == '1'
    with pytest.raises(SystemExit):
        main(['--root', str(root), '--owner-ref', 'wrong-owner', '--durability', 'required',
              'capabilities'])
    assert 'checkpoint_owner_mismatch' in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(['--root', str(root), '--durability', 'legacy', 'capabilities'])
    assert 'checkpoint_required' in capsys.readouterr().err


def test_required_cli_refuses_unreconciled_history_without_changing_mode(tmp_path, capsys):
    bridge = prepared(tmp_path, durable=False)
    execution(bridge)
    bridge.store.finish('turn-1', 'completed')
    before = bridge.checkpoints.identity()
    with pytest.raises(SystemExit):
        main(['--root', str(bridge.root), '--owner-ref', 'fixture-owner',
              '--durability', 'required', 'rpc'])
    assert json.loads(capsys.readouterr().err)['error'] == 'checkpoint_upgrade_required'
    assert Bridge(bridge.root).checkpoints.identity() == before
    main(['--root', str(bridge.root), 'capabilities'])
    assert json.loads(capsys.readouterr().out)['durability']['support'] == 'disabled'
