"""Cold Store inspection must never turn unknown legacy material into apparent absence."""

import pytest
import sqlite3

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
    with Bridge(root, owner_ref='fixture-owner', durable=True) as bridge:
        backend = bridge.store.storage.name
    assert inspect(root) == {'state': 'present'}
    database = root / ('store-backend.json' if backend == 'postgresql' else 'bridge.sqlite3')
    database.rename(root / 'saved')
    database.symlink_to(root / 'saved')
    with pytest.raises(BridgeError):
        inspect(root)


def test_legacy_probe_is_read_only_and_never_migrates(tmp_path):
    root = tmp_path / 'legacy'
    root.mkdir(mode=0o700)
    database = root / 'bridge.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.executescript('CREATE TABLE metadata(version); INSERT INTO metadata VALUES(13)')
    database.chmod(0o600)
    before = database.read_bytes()
    with pytest.raises(BridgeError) as legacy:
        inspect(root)
    assert legacy.value.code == 'native_identity_unknown'
    assert database.read_bytes() == before and set(root.iterdir()) == {database}


@pytest.mark.parametrize('rows', [[], [None], ['unknown'], [15, 15]])
def test_malformed_metadata_is_rejected_without_migration(tmp_path, rows):
    import sqlite3

    root = tmp_path / 'state'
    root.mkdir(mode=0o700)
    database = root / 'bridge.sqlite3'
    connection = sqlite3.connect(database)
    connection.execute('CREATE TABLE metadata(version)')
    connection.executemany('INSERT INTO metadata VALUES (?)', [(row,) for row in rows])
    connection.commit()
    connection.close()
    database.chmod(0o600)
    before = database.read_bytes()
    with pytest.raises(BridgeError, match='Store schema') as caught:
        inspect(root)
    assert caught.value.code == 'native_identity_unknown'
    assert database.read_bytes() == before
