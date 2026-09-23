"""Automatically routed Codex sessions own a private account-independent home."""

import os
from pathlib import Path

import pytest

from agentbridge.errors import BridgeError
from agentbridge.proxy import session_home


def test_session_home_is_stable_private_and_has_no_created_config(tmp_path):
    first = Path(session_home(tmp_path, "session_1"))
    second = Path(session_home(tmp_path, "session_1"))
    assert first == second == tmp_path / "codex-runtime" / "session_1"
    assert first.is_dir()
    assert os.stat(first).st_mode & 0o777 == 0o700
    assert os.stat(first.parent).st_mode & 0o777 == 0o700
    assert list(first.iterdir()) == []


def test_session_home_rejects_invalid_id_and_symbolic_links(tmp_path):
    with pytest.raises(BridgeError) as error:
        session_home(tmp_path, "../outside")
    assert error.value.code == "invalid_id"

    target = tmp_path / "other"
    target.mkdir()
    (tmp_path / "codex-runtime").symlink_to(target, target_is_directory=True)
    with pytest.raises(BridgeError) as error:
        session_home(tmp_path, "session_1")
    assert error.value.code == "unsafe_store"


def test_session_home_rejects_symlink_leaf_and_store_root(tmp_path):
    runtime = tmp_path / "codex-runtime"
    runtime.mkdir()
    target = tmp_path / "other"
    target.mkdir()
    (runtime / "session_1").symlink_to(target, target_is_directory=True)
    with pytest.raises(BridgeError) as error:
        session_home(tmp_path, "session_1")
    assert error.value.code == "unsafe_store"

    root_link = tmp_path.parent / "root_link"
    root_link.symlink_to(tmp_path, target_is_directory=True)
    try:
        with pytest.raises(BridgeError) as error:
            session_home(root_link, "session_2")
        assert error.value.code == "unsafe_store"
    finally:
        root_link.unlink()
