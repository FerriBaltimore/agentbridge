"""Keep model workspaces separate from private bridge and host state."""

import os
from pathlib import Path
import sys

from .errors import BridgeError


_HOST_PRIVATE_ROOTS = (Path('/proc'), Path('/run'), Path('/dev'), Path('/sys'))
_BROAD_ROOTS = (Path('/'), Path('/home'), Path('/tmp'))
_EXPOSED_SYSTEM_ROOTS = tuple(Path(name).resolve() for name in
                              ('/usr', '/lib', '/lib64', '/bin', '/sbin'))


def validate_private_state_root(state_root):
    """System dependencies must never grant incidental access to bridge state."""
    root = Path(state_root).resolve()
    if any(root.is_relative_to(system) for system in _EXPOSED_SYSTEM_ROOTS):
        raise BridgeError('unsafe_store',
                          'Private AgentBridge state must be outside system runtime directories.')


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
    validate_private_state_root(state_root)
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
