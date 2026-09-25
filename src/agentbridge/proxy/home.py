"""Private, stable Codex state directory for an automatically routed session."""

import os
from pathlib import Path
import shutil

from ..errors import BridgeError
from ..models import identifier


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def session_home(store_root, session_id: str) -> str:
    """Create `<store_root>/codex-runtime/<session_id>` with mode 0700.

    The directory and native thread belong to the instance, independently of
    the provider account. Every later turn resumes that thread even when the
    proxy account or model changes. It contains
    no AgentBridge-created credentials or proxy configuration.
    """
    identifier(session_id)
    if not isinstance(store_root, (str, os.PathLike)):
        raise BridgeError("unsafe_store", "The store root must be an existing directory.")
    root = Path(store_root).expanduser().absolute()
    try:
        if root != root.resolve(strict=True):
            raise BridgeError("unsafe_store", "The store root must be a canonical directory.")
        if (hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd
                and os.mkdir in os.supports_dir_fd):
            _prepare_with_descriptors(root, session_id)
        else:
            _prepare_portable(root, session_id)
    except (OSError, RuntimeError):
        raise BridgeError("unsafe_store", "The Codex session home could not be secured.") from None
    return str(root / "codex-runtime" / session_id)


def remove_session_home(store_root, session_id, *, error_code='instance_cleanup_failed',
                        retry_action='deletion'):
    """Remove only one private Codex session home, without following symlinks."""
    identifier(session_id)
    runtime = Path(store_root) / 'codex-runtime'
    if runtime.is_symlink():
        raise BridgeError('unsafe_store', 'The Codex runtime directory is unsafe.')
    if not runtime.exists():
        return
    if not runtime.is_dir() or runtime.resolve() != runtime:
        raise BridgeError('unsafe_store', 'The Codex runtime directory is unsafe.')
    home = runtime / session_id
    if home.is_symlink():
        raise BridgeError('unsafe_store', 'The instance home is unsafe.')
    if not home.exists():
        return
    if not home.is_dir() or not shutil.rmtree.avoids_symlink_attacks:
        raise BridgeError('unsafe_store', 'The instance home is unsafe.')
    try:
        shutil.rmtree(home)
    except OSError:
        raise BridgeError(error_code,
                          f'The instance home could not be removed; retry {retry_action}.') from None


def _prepare_with_descriptors(root: Path, session_id: str):
    current = os.open(root.anchor, _DIRECTORY_FLAGS)
    try:
        for component in root.parts[1:]:
            following = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = following
        runtime = _private_child(current, "codex-runtime")
        try:
            home = _private_child(runtime, session_id)
            os.close(home)
        finally:
            os.close(runtime)
    finally:
        os.close(current)


def _private_child(parent: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    child = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    os.fchmod(child, 0o700)
    return child


def _prepare_portable(root: Path, session_id: str):
    # dir_fd is unavailable on some platforms. Refuse symbolic links before
    # touching each component, then enforce private permissions where supported.
    for component in (root, *root.parents):
        if component.is_symlink():
            raise BridgeError("unsafe_store", "The store root cannot contain symbolic links.")
    if not root.is_dir():
        raise BridgeError("unsafe_store", "The store root must be an existing directory.")
    for child in (root / "codex-runtime", root / "codex-runtime" / session_id):
        if child.is_symlink():
            raise BridgeError("unsafe_store", "The Codex session home cannot be a symbolic link.")
        child.mkdir(mode=0o700, exist_ok=True)
        if not child.is_dir() or child.is_symlink():
            raise BridgeError("unsafe_store", "The Codex session home must be a directory.")
        os.chmod(child, 0o700)
