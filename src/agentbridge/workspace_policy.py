"""Keep model workspaces separate from private bridge and host state."""

import os
from pathlib import Path
import sys

from .errors import BridgeError


_HOST_PRIVATE_ROOTS = (Path('/proc'), Path('/run'), Path('/dev'), Path('/sys'))
_BROAD_ROOTS = (Path('/'), Path('/home'), Path('/tmp'))


def overlaps_private_state(workspace, state_root):
    """Return whether granting a process the workspace exposes private state."""
    roots = (Path(state_root), *_HOST_PRIVATE_ROOTS)
    return (workspace in _BROAD_ROOTS
            or any(workspace.is_relative_to(root) or root.is_relative_to(workspace)
                   for root in roots))


def validate_workspace(path, state_root):
    """Resolve a workspace before an account is selected or work is admitted."""
    workspace = Path(path).expanduser().resolve()
    if not workspace.is_dir():
        raise BridgeError('invalid_workspace', 'Workspace must be an existing directory.')
    if overlaps_private_state(workspace, Path(state_root).resolve()):
        raise BridgeError('invalid_workspace',
                          'Workspace must not include private AgentBridge or host state.')
    return workspace


def writable_runtime_in_workspace(workspace):
    """Detect launch code that a workspace-writing Codex could replace."""
    runtime_paths = [Path(sys.executable).absolute(), Path(sys.executable).resolve(),
                     Path(sys.prefix).absolute(), Path(__file__).absolute().parents[1],
                     Path(__file__).resolve().parents[1]]
    proxy_binary = os.environ.get('AGENTBRIDGE_CLIPROXY_BIN')
    if proxy_binary and Path(proxy_binary).is_absolute():
        runtime_paths.extend((Path(proxy_binary).absolute(), Path(proxy_binary).resolve()))
    return any(path.is_relative_to(workspace) for path in runtime_paths)


def validate_execution_workspace(path, state_root, *, workspace_write):
    workspace = validate_workspace(path, state_root)
    if workspace_write and writable_runtime_in_workspace(workspace):
        raise BridgeError('invalid_workspace',
                          'Writable workspace contains the AgentBridge runtime; '
                          'install it outside the workspace.')
    return workspace
