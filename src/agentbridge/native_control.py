"""Scoped requests to the existing native turn owner, without another process."""

from contextlib import contextmanager
import json
import os
import socket
import struct
import stat

from .errors import BridgeError
from .error_evidence import from_issue
from .provider_errors import CANONICAL, NATIVE_CODES
from .models import identifier
from .native_observations import thread
from .process import alive
from .queueing.connection import directory
from .queueing.interruption import QueuedInterruption


MAX_REPLY = 8 * 1024 * 1024
NATIVE_METHODS = frozenset(('initialize', 'thread_resume', 'thread_read',
                           'turn_start', 'turn_steer', 'turn_interrupt'))


def _error_details(value, code):
    """Only bounded provider diagnostics cross the live owner's socket boundary."""
    if not isinstance(value, dict):
        return {}
    result = {}
    detection = value.get('detection')
    if isinstance(detection, str) and detection in {'structured', 'text_match', 'http_status', 'unclassified'}:
        result['detection'] = detection
    status = value.get('http_status')
    if type(status) is int and 400 <= status < 600:
        result['http_status'] = status
    provider_code = value.get('provider_code')
    if (isinstance(provider_code, str) and
            (NATIVE_CODES.get(provider_code) == code or provider_code in CANONICAL and provider_code == code)):
        result['provider_code'] = provider_code
    evidence = from_issue({'details': value})
    if evidence is not None:
        result['unknown_evidence'] = evidence
    method = value.get('native_method')
    if isinstance(method, str) and method in NATIVE_METHODS:
        result['native_method'] = method
    number = value.get('native_code')
    if type(number) is int and -(2 ** 31) <= number < 2 ** 31:
        result['native_code'] = number
    return result


@contextmanager
def address(root, instance_id, turn_id):
    identifier(turn_id)
    folder = directory(root, instance_id)
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        yield folder / ('native-' + turn_id + '.sock'), (
            f'/proc/self/fd/{descriptor}/native-{turn_id}.sock')
    finally:
        os.close(descriptor)


def cleanup(store, run):
    """Remove only a stopped owner's abandoned endpoint; never release a live owner."""
    if alive(run['child_pid'], run['child_identity']):
        return
    with address(store.root, run['session_id'], run['id']) as (path, _):
        try:
            current = path.lstat()
            if not stat.S_ISSOCK(current.st_mode):
                raise BridgeError('native_stop_unverified', 'The native endpoint owner is unverified.')
            verified = path.lstat()
            if (current.st_dev, current.st_ino) == (verified.st_dev, verified.st_ino):
                path.unlink()
        except FileNotFoundError:
            pass


def request(store, run, action, *, include_turns=True):
    if not alive(run['child_pid'], run['child_identity']):
        raise BridgeError('native_connection_unavailable', 'The native turn owner is unavailable.')
    payload = {'action': action, 'turn_id': run['id'], 'instance_id': run['session_id'],
               'include_turns': include_turns}
    sent = False
    try:
        with address(store.root, run['session_id'], run['id']) as (_, path):
            with socket.socket(socket.AF_UNIX) as channel:
                channel.settimeout(3)
                channel.connect(path)
                pid, uid, _ = struct.unpack('3i', channel.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                if (pid != run['child_pid'] or uid != os.getuid()
                        or not alive(pid, run['child_identity'])):
                    raise BridgeError('native_owner_changed', 'The native execution owner changed.')
                sent = True
                channel.sendall((json.dumps(payload) + '\n').encode())
                with channel.makefile('rb') as stream:
                    line = stream.readline(MAX_REPLY + 1)
                if len(line) > MAX_REPLY or not line.endswith(b'\n'):
                    raise ValueError('Invalid reply')
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError('Invalid reply')
        if value.get('ok') is not True:
            raise BridgeError(value.get('code', 'native_connection_unavailable'),
                              'The native operation was not confirmed.', phase='execution',
                              outcome='unknown' if action == 'interrupt' else 'not_started',
                              details=_error_details(value.get('details'), value.get('code')))
        return value['result']
    except (OSError, ValueError, KeyError):
        raise BridgeError('native_connection_unavailable',
                          'The native operation could not be observed; execution may continue.',
                          phase='execution', outcome='unknown' if sent and action == 'interrupt'
                          else 'not_started') from None


class NativeControl:
    def __init__(self, store, run_id, control, redactor):
        self.store, self.run_id = store, run_id
        self.control, self.redactor = control, redactor
        self.instance_id = store.get('runs', run_id)['session_id']
        self.queued_interruption = QueuedInterruption(store, run_id, control)
        self.server, self.path, self.file_identity = None, None, None

    def __enter__(self):
        self.server = socket.socket(socket.AF_UNIX)
        try:
            with address(self.store.root, self.instance_id, self.run_id) as (path, endpoint):
                self.path = path
                self.server.bind(endpoint)
                current = path.stat()
                self.file_identity = (current.st_dev, current.st_ino)
            self.server.listen(8)
            self.server.setblocking(False)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.server is not None:
            self.server.close()
        if self.file_identity is not None:
            try:
                current = self.path.stat()
                if (current.st_dev, current.st_ino) == self.file_identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass

    def poll(self):
        self.queued_interruption.poll()
        try:
            channel, _ = self.server.accept()
        except BlockingIOError:
            return
        with channel:
            channel.settimeout(.25)
            try:
                _, uid, _ = struct.unpack('3i', channel.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                if uid != os.getuid():
                    raise ValueError('Wrong owner')
                with channel.makefile('rb') as stream:
                    line = stream.readline(4097)
                if len(line) > 4096 or not line.endswith(b'\n'):
                    raise ValueError('Invalid request')
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError('Invalid request')
                run = self.store.get('runs', self.run_id)
                if (value.get('turn_id') != self.run_id
                        or value.get('instance_id') != self.instance_id
                        or run['child_pid'] != os.getpid()):
                    raise BridgeError('native_owner_changed', 'The native execution owner changed.')
                result = self.execute(value)
                reply = {'ok': True, 'result': self.redactor.clean(result)}
            except (BridgeError, OSError, ValueError, KeyError) as error:
                reply = {'ok': False, 'code': (error.code if isinstance(error, BridgeError)
                                               else 'invalid_request')}
                if isinstance(error, BridgeError):
                    reply['details'] = _error_details(error.details, error.code)
            try:
                channel.sendall((json.dumps(reply, allow_nan=False) + '\n').encode())
            except OSError:
                pass  # The caller detached; the native owner and execution remain alive.

    def execute(self, value):
        control = self.control
        if control.thread_id is None:
            raise BridgeError('native_connection_unavailable', 'The native thread is still opening.')
        if value.get('action') == 'interrupt':
            if control.turn_id is None:
                raise BridgeError('native_connection_unavailable', 'The native turn is still opening.')
            control.rpc('turn/interrupt', {'threadId': control.thread_id,
                                          'turnId': control.turn_id}, timeout=2)
            return {'native_session_id': control.thread_id,
                    'native_turn_id': control.turn_id, 'acknowledged': True}
        if value.get('action') not in {'read', 'reopen'}:
            raise ValueError('Unknown native operation')
        include_turns = value.get('include_turns', True)
        if type(include_turns) is not bool:
            raise ValueError('Invalid history selection')
        result = control.rpc('thread/read', {'threadId': control.thread_id,
                                             'includeTurns': include_turns}, timeout=2)
        observed = thread(result.get('thread'))
        if observed['native_session_id'] != control.thread_id:
            raise BridgeError('native_session_diverged', 'The native thread identity changed.')
        if control.turn_id is not None:
            observed['owner'] = {'turn_id': self.run_id, 'native_turn_id': control.turn_id}
        return observed
