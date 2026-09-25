"""Select the native isolation boundary without changing execution policy.

Normal restricted turns use a private filesystem and PID namespace compatible
with Codex's own command sandbox. Selected inputs disable native shell tools
and use Landlock, including hosts which disable nested user namespaces.
Full access keeps the process inspection filter but uses host file permissions;
it is not a selected-input or tenant isolation boundary.
"""

import ctypes
import os
from pathlib import Path
import shutil
import struct
import sys

from .native_process_guard import restrict_process_inspection
from .workspace_policy import (overlaps_private_state, validate_private_state_root,
                               writable_runtime_in_workspace)
from .errors import BridgeError


_CREATE_RULESET = 444
_ADD_RULE = 445
_RESTRICT_SELF = 446
_RULE_PATH_BENEATH = 1
_GET_ABI = 1
_PR_SET_NO_NEW_PRIVS = 38

_EXECUTE = 1 << 0
_WRITE_FILE = 1 << 1
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_REMOVE_DIR = 1 << 4
_REMOVE_FILE = 1 << 5
_MAKE_CHAR = 1 << 6
_MAKE_DIR = 1 << 7
_MAKE_REG = 1 << 8
_MAKE_SOCK = 1 << 9
_MAKE_FIFO = 1 << 10
_MAKE_BLOCK = 1 << 11
_MAKE_SYM = 1 << 12
_REFER = 1 << 13
_TRUNCATE = 1 << 14
_IOCTL_DEV = 1 << 15
_READ = _EXECUTE | _READ_FILE | _READ_DIR
_DATA = _READ_FILE | _READ_DIR
_WRITE = (_WRITE_FILE | _REMOVE_DIR | _REMOVE_FILE | _MAKE_CHAR | _MAKE_DIR
          | _MAKE_REG | _MAKE_SOCK | _MAKE_FIFO | _MAKE_BLOCK | _MAKE_SYM
          | _REFER | _TRUNCATE | _IOCTL_DEV)
_HANDLED = _READ | _WRITE


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def available():
    """Require ABI 5 for all filesystem rights used by this policy."""
    if not sys.platform.startswith('linux'):
        return False
    return _libc().syscall(_CREATE_RULESET, None, 0, _GET_ABI) >= 5


def _canonical(path, *, directory=True):
    target = Path(path)
    if not target.is_absolute() or target != target.resolve(strict=True):
        raise ValueError('Sandbox paths must be canonical and absolute')
    if directory and not target.is_dir():
        raise ValueError('Sandbox directory is unavailable')
    if not directory and not target.is_file():
        raise ValueError('Sandbox file is unavailable')
    return target


def _rule(libc, ruleset_fd, path, access):
    path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        rule = ctypes.create_string_buffer(struct.pack('=Qi', access, path_fd))
        if libc.syscall(_ADD_RULE, ruleset_fd, _RULE_PATH_BENEATH,
                        ctypes.byref(rule), 0) != 0:
            raise OSError(ctypes.get_errno(), 'Landlock path rule failed')
    finally:
        os.close(path_fd)


def restrict(*, cwd, home, temporary, executable, inputs_only=True,
             workspace_write=False, mcp_enabled=False):
    """Allow only selected native paths and deny worker process inspection."""
    if not available():
        raise OSError('Required Landlock filesystem isolation is unavailable')
    cwd = _canonical(cwd)
    home = _canonical(home)
    temporary = _canonical(temporary)
    executable = _canonical(executable, directory=False)
    if home.parent.name != 'codex-runtime' or home.name in ('', '.', '..'):
        raise ValueError('Codex state must be scoped to one instance')
    state_root = home.parent.parent
    validate_private_state_root(state_root)
    if any(executable.is_relative_to(path)
           for path in (cwd, temporary, home)):
        raise ValueError('Native executable must be outside writable runtime paths')
    bundle_directory = None
    if executable.is_relative_to(state_root):
        from .bundle.runtime import is_bundled_codex
        bundle_directory = is_bundled_codex(executable, state_root)
        if bundle_directory is None:
            raise ValueError('Native executable must be outside writable runtime paths')
    if temporary == Path('/tmp'):
        raise ValueError('Native temporary path must be private')
    if overlaps_private_state(cwd, state_root):
        raise ValueError('Workspace includes private worker state')
    if workspace_write and writable_runtime_in_workspace(cwd):
        raise ValueError('Writable workspace includes the AgentBridge runtime')
    if inputs_only and workspace_write:
        raise ValueError('Inputs-only workspace cannot be writable')
    if inputs_only and any(cwd.iterdir()):
        raise ValueError('Inputs-only workspace must be empty')
    libc = _libc()
    attr = ctypes.create_string_buffer(struct.pack('=QQQ', _HANDLED, 0, 0))
    ruleset_fd = libc.syscall(_CREATE_RULESET, ctypes.byref(attr), 24, 0)
    if ruleset_fd < 0:
        raise OSError(ctypes.get_errno(), 'Landlock ruleset creation failed')
    try:
        for system in ('/usr', '/lib', '/lib64'):
            if Path(system).exists():
                _rule(libc, ruleset_fd, system, _READ)
        for path in ('/etc/ssl/certs', '/etc/hosts', '/etc/resolv.conf',
                     '/etc/nsswitch.conf', '/etc/passwd', '/etc/group',
                     '/dev/urandom', '/dev/zero'):
            if Path(path).exists():
                _rule(libc, ruleset_fd, path, _READ_FILE)
        if Path('/dev/null').exists():
            _rule(libc, ruleset_fd, '/dev/null', _READ_FILE | _WRITE_FILE)
        _rule(libc, ruleset_fd, cwd, _READ | (_WRITE if workspace_write else 0))
        _rule(libc, ruleset_fd, executable, _EXECUTE | _READ_FILE)
        if bundle_directory is not None:
            _rule(libc, ruleset_fd, bundle_directory, _READ)
        _rule(libc, ruleset_fd, home, _DATA | _WRITE)
        _rule(libc, ruleset_fd, temporary, _DATA | _WRITE)
        if mcp_enabled:
            source = _canonical(Path(__file__).resolve().parents[1])
            if state_root.is_relative_to(source):
                raise ValueError('MCP source includes private worker state')
            _rule(libc, ruleset_fd, source, _READ)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'No-new-privileges setup failed')
        if libc.syscall(_RESTRICT_SELF, ruleset_fd, 0) != 0:
            raise OSError(ctypes.get_errno(), 'Landlock enforcement failed')
        restrict_process_inspection()
    finally:
        os.close(ruleset_fd)


def wrap(command, *, inputs_only=False, workspace_write=False, mcp_enabled=False,
         full_access=False, selected_context=False):
    """Run Codex through this policy before either native transport starts."""
    if full_access and (inputs_only or mcp_enabled or selected_context):
        raise BridgeError('invalid_execution_policy',
                          'Full access cannot disable selected input isolation.')
    flags = ['--inputs-only' if inputs_only else '--normal']
    if full_access:
        flags.append('--full-access')
    if workspace_write:
        flags.append('--write-workspace')
    if mcp_enabled:
        flags.append('--mcp')
    if inputs_only or mcp_enabled or selected_context:
        flags.append('--no-native-shell')
    return [sys.executable, '-P', '-m', 'agentbridge.native_sandbox',
            *flags, '--', *command]


def main():
    arguments = sys.argv[1:]
    if not arguments:
        raise SystemExit(2)
    inputs_only = True
    workspace_write = mcp_enabled = full_access = False
    native_shell = True
    if arguments[0] in {'--inputs-only', '--normal'}:
        inputs_only = arguments.pop(0) == '--inputs-only'
        while arguments and arguments[0] != '--':
            flag = arguments.pop(0)
            if flag == '--write-workspace':
                workspace_write = True
            elif flag == '--mcp':
                mcp_enabled = True
            elif flag == '--full-access':
                full_access = True
            elif flag == '--no-native-shell':
                native_shell = False
            else:
                raise SystemExit(2)
        if not arguments or arguments.pop(0) != '--' or not arguments:
            raise SystemExit(2)
    command = arguments
    selected = shutil.which(command[0])
    if selected is None:
        raise SystemExit(1)
    executable = str(Path(selected).resolve(strict=True))
    try:
        if not inputs_only and not full_access and native_shell:
            from .native_namespace import reexec
            reexec(command, home=os.environ['CODEX_HOME'], temporary=os.environ['TMPDIR'],
                   workspace_write=workspace_write, mcp_enabled=mcp_enabled)
            raise RuntimeError('The native namespace launcher returned without execution')
        if full_access:
            if inputs_only or mcp_enabled:
                raise ValueError('Full access conflicts with selected input isolation')
            # Explicit trusted execution uses host filesystem permissions.
            # This filter remains defense in depth, not tenant isolation.
            restrict_process_inspection()
        else:
            restrict(cwd=os.getcwd(), home=os.environ['CODEX_HOME'],
                     temporary=os.environ['TMPDIR'], executable=executable,
                     inputs_only=inputs_only, workspace_write=workspace_write,
                     mcp_enabled=mcp_enabled)
        os.execvpe(executable, command, os.environ)
    except (OSError, ValueError, KeyError, RuntimeError, BridgeError):
        # Stderr belongs to the private native channel and is never persisted.
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
