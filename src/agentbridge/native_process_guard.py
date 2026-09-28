"""Deny native provider inspection of same-user worker and proxy processes."""

import ctypes
import errno


_ALLOW = 0x7FFF0000
_ERRNO = 0x00050000 | errno.EPERM
_DENIED = ('ptrace', 'process_vm_readv', 'process_vm_writev',
           'pidfd_getfd', 'kcmp')


def restrict_process_inspection():
    """Install a required inherited seccomp filter before native exec."""
    library = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    library.seccomp_init.argtypes = (ctypes.c_uint32,)
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = (ctypes.c_void_p, ctypes.c_uint32,
                                         ctypes.c_int, ctypes.c_uint)
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_load.argtypes = (ctypes.c_void_p,)
    library.seccomp_load.restype = ctypes.c_int
    library.seccomp_release.argtypes = (ctypes.c_void_p,)
    context = library.seccomp_init(_ALLOW)
    if not context:
        raise OSError('Native process inspection filter could not be created')
    try:
        for name in _DENIED:
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number < 0 or library.seccomp_rule_add(context, _ERRNO, number, 0) != 0:
                raise OSError('Native process inspection filter could not be configured')
        if library.seccomp_load(context) != 0:
            raise OSError('Native process inspection filter could not be installed')
    finally:
        library.seccomp_release(context)


def namespace_filter_fd():
    """Export an inherited native policy; bubblewrap loads it after namespace setup."""
    import os
    import struct

    library = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    library.seccomp_init.argtypes = (ctypes.c_uint32,)
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add_array.argtypes = (
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
        ctypes.c_void_p)
    library.seccomp_rule_add_array.restype = ctypes.c_int
    library.seccomp_export_bpf.argtypes = (ctypes.c_void_p, ctypes.c_int)
    library.seccomp_export_bpf.restype = ctypes.c_int
    library.seccomp_release.argtypes = (ctypes.c_void_p,)
    context = library.seccomp_init(_ALLOW)
    if not context:
        raise OSError('Native namespace filter could not be created')
    descriptor = -1
    try:
        for name in (*_DENIED, 'unshare', 'setns'):
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number < 0 or library.seccomp_rule_add_array(
                    context, _ERRNO, number, 0, None) != 0:
                raise OSError('Native namespace filter could not be configured')
        # clone3 hides its flags in a pointer. ENOSYS preserves libc's ordinary
        # thread/process fallback to clone, where CLONE_NEWUSER is inspected.
        number = library.seccomp_syscall_resolve_name(b'clone3')
        if number < 0 or library.seccomp_rule_add_array(
                context, 0x00050000 | errno.ENOSYS, number, 0, None) != 0:
            raise OSError('Native namespace filter could not be configured')
        number = library.seccomp_syscall_resolve_name(b'clone')
        comparison = ctypes.create_string_buffer(
            struct.pack('=IIQQ', 0, 7, 0x10000000, 0x10000000))
        if number < 0 or library.seccomp_rule_add_array(
                context, _ERRNO, number, 1, ctypes.byref(comparison)) != 0:
            raise OSError('Native namespace filter could not be configured')
        descriptor = os.memfd_create('agentbridge-native-policy', os.MFD_CLOEXEC)
        if library.seccomp_export_bpf(context, descriptor) != 0:
            raise OSError('Native namespace filter could not be exported')
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.set_inheritable(descriptor, True)
        return descriptor
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        library.seccomp_release(context)
