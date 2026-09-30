"""Cold Store inspection must never turn unknown legacy material into apparent absence."""

import pytest

from agentbridge import Bridge
from agentbridge.checkpoint.availability import inspect
from agentbridge.errors import BridgeError


def test_inspection_does_not_create_database_and_rejects_unknown_material(tmp_path):
    root = tmp_path / 'state'
    assert inspect(root) == {'state': 'absent'} and not root.exists()
    root.mkdir(mode=0o700)
    assert inspect(root) == {'state': 'absent'} and not list(root.iterdir())
    private = root / 'unknown-vault'
    private.write_bytes(b'synthetic private material')
    with pytest.raises(BridgeError) as unknown:
        inspect(root)
    assert unknown.value.code == 'native_identity_unknown'
    private.unlink()
    with Bridge(root):
        pass
    assert inspect(root) == {'state': 'present'}
    database = root / 'bridge.sqlite3'
    database.rename(root / 'saved')
    database.symlink_to(root / 'saved')
    with pytest.raises(BridgeError):
        inspect(root)
