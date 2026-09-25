"""Project only approved native filesystem paths into a private process namespace."""

import os
from pathlib import Path
import shutil

from .bundle.runtime import is_bundled_codex
from .native_process_guard import restrict_process_inspection
from .workspace_policy import (overlaps_private_state, validate_private_state_root,
                               writable_runtime_in_workspace)


def _directory(value):
    path = Path(value)
    if not path.is_absolute() or path != path.resolve(strict=True) or not path.is_dir():
        raise ValueError('Native namespace directories must be canonical and absolute')
    return path


def _launcher(command, home, temporary):
    if not command:
        raise ValueError('A native command is required')
    selected = shutil.which(command[0])
    if selected is None:
        raise ValueError('The native executable is unavailable')
    executable = Path(selected).resolve(strict=True)
    roots = (Path.cwd(), home, temporary)
    if any(executable.is_relative_to(root) for root in roots):
        raise ValueError('The native executable must be outside writable runtime paths')
    package = is_bundled_codex(executable, home.parent.parent)
    if executable.is_relative_to(home.parent.parent) and package is None:
        raise ValueError('The native executable is not a verified bundled runtime')
    path = package / 'codex-resources/bwrap' if package else Path('/usr/bin/bwrap')
    if (not path.is_absolute() or path != path.resolve(strict=True)
            or not path.is_file() or not os.access(path, os.X_OK)
            or any(path.is_relative_to(root) for root in roots)):
        raise ValueError('The native namespace launcher is unavailable or unsafe')
    return path, executable, package


def _system_mounts():
    args = []
    for name in ('/usr', '/lib', '/lib64', '/bin', '/sbin'):
        path = Path(name)
        if not path.exists():
            continue
        if path.is_symlink():
            args.extend(('--symlink', os.readlink(path), name))
        else:
            args.extend(('--ro-bind', name, name))
    for name in ('/etc/ssl/certs', '/etc/hosts', '/etc/resolv.conf',
                 '/etc/nsswitch.conf', '/etc/passwd', '/etc/group', '/etc/ld.so.cache'):
        path = Path(name)
        if path.exists():
            args.extend(('--ro-bind', str(path.resolve(strict=True)), name))
    return args


def reexec(command, *, home, temporary, workspace_write=False, mcp_enabled=False):
    """Launch ordinary Codex with a projected filesystem and private procfs."""
    home, temporary = _directory(home), _directory(temporary)
    cwd = _directory(Path.cwd())
    if mcp_enabled:
        raise ValueError('Selected MCP inputs require their separate isolation policy')
    if home.parent.name != 'codex-runtime' or temporary == Path('/tmp'):
        raise ValueError('Native state and temporary paths must be private')
    validate_private_state_root(home.parent.parent)
    if overlaps_private_state(cwd, home.parent.parent):
        raise ValueError('Workspace includes private worker state')
    if workspace_write and writable_runtime_in_workspace(cwd):
        raise ValueError('Writable workspace includes the AgentBridge runtime')
    launcher, executable, package = _launcher(command, home, temporary)
    # Start with bubblewrap's empty filesystem, without host-root or host-proc
    # mounts. Keep the existing network namespace so the proxy stays reachable.
    args = [str(launcher), '--unshare-user', '--unshare-pid',
            '--uid', str(os.getuid()), '--gid', str(os.getgid()),
            '--die-with-parent', '--cap-drop', 'ALL', *_system_mounts(),
            '--dev', '/dev', '--proc', '/proc',
            '--bind' if workspace_write else '--ro-bind', str(cwd), str(cwd),
            '--bind', str(home), str(home), '--chdir', str(cwd),
            '--bind', str(temporary), str(temporary)]
    native_root = package or executable
    args.extend(('--ro-bind', str(native_root), str(native_root),
                 '--remount-ro', '/',
                 '--', str(executable), *command[1:]))
    restrict_process_inspection()
    os.execv(str(launcher), args)
