"""Restrict an inputs-only Codex subprocess to its selected input paths.

The AgentBridge worker must keep its database and proxy account state mounted.
This launcher applies Landlock *after* the worker has prepared Codex state, so
the native provider cannot read the worker's other files. A kernel without the
required Landlock rights fails closed before launching the provider.
"""

import ctypes
import os
from pathlib import Path
import shutil
import struct
import sys


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


def restrict(*, cwd, home, temporary, executable):
    """Apply a read allowlist and private writable homes before native exec."""
    if not available():
        raise OSError('Required Landlock filesystem isolation is unavailable')
    cwd = _canonical(cwd)
    home = _canonical(home)
    temporary = _canonical(temporary)
    executable = _canonical(executable, directory=False)
    if temporary == Path('/tmp') or home == Path('/state'):
        raise ValueError('Inputs-only paths must be private')
    if not home.is_relative_to(Path('/state/codex-runtime')) and Path('/state').exists():
        raise ValueError('Codex state must be scoped to one instance')
    if cwd in (Path('/'), Path('/state'), Path('/home'), Path('/tmp')):
        raise ValueError('Inputs-only workspace is too broad')
    if any(cwd.iterdir()):
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
                     '/dev/null', '/dev/urandom', '/dev/zero'):
            if Path(path).exists():
                _rule(libc, ruleset_fd, path, _READ_FILE)
        _rule(libc, ruleset_fd, cwd, _READ)
        _rule(libc, ruleset_fd, executable, _EXECUTE | _READ_FILE)
        _rule(libc, ruleset_fd, home, _DATA | _WRITE)
        _rule(libc, ruleset_fd, temporary, _DATA | _WRITE)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), 'No-new-privileges setup failed')
        if libc.syscall(_RESTRICT_SELF, ruleset_fd, 0) != 0:
            raise OSError(ctypes.get_errno(), 'Landlock enforcement failed')
    finally:
        os.close(ruleset_fd)


def main():
    if len(sys.argv) < 2:
        raise SystemExit(2)
    command = sys.argv[1:]
    selected = shutil.which(command[0])
    if selected is None:
        raise SystemExit(1)
    executable = str(Path(selected).resolve(strict=True))
    try:
        restrict(cwd=os.getcwd(), home=os.environ['CODEX_HOME'],
                 temporary=os.environ['TMPDIR'], executable=executable)
        os.execvpe(executable, command, os.environ)
    except (OSError, ValueError, KeyError):
        # Stderr belongs to the private native channel and is never persisted.
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
