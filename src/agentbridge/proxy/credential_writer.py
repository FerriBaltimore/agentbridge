"""Supervisor-side access to the pinned writer's private snapshot/revision protocol."""

import http.client
import json
import os
import tempfile

from ..checkpoint import content, native, state
from ..errors import BridgeError
from ..models import identifier
from .credential_writer_content import MAX_ARCHIVE, MAX_BYTES, MAX_FILES, read_export, revision


def export_path(directory, account_id, capture_id):
    identifier(account_id)
    state.canonical_uuid(capture_id)
    root = native.private_directory(directory / 'captures' / capture_id)
    return root / (account_id + '.tar')


def describe(path, capture_id):
    value, _ = read_export(path)
    checksum, size = native.digest(path)
    return {'capture_id': capture_id, 'revision': value, 'sha256': checksum, 'bytes': size}


def request(supervisor, action, account_id, capture_id=None):
    """Runs in the sidecar network namespace; IPC returns references, never credential bytes."""
    if action == 'credential_snapshot':
        target = export_path(supervisor.directory, account_id, capture_id)
        if target.exists():
            return describe(target, capture_id)
    elif action != 'credential_revision' or capture_id is not None:
        raise BridgeError('invalid_request', 'Unknown credential writer operation.')
    route = supervisor.route('ensure', account_id)
    port = int(route['proxy_base_url'].split(':')[2].split('/')[0])
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
    temporary = None
    try:
        method = 'snapshot' if action == 'credential_snapshot' else 'revision'
        connection.request('GET', '/v0/management/auth-files/' + method,
                           headers={'Authorization': 'Bearer ' + route['management_key']})
        response = connection.getresponse()
        if response.status in {404, 405}:
            raise BridgeError('credential_snapshot_unsupported',
                              'This credential writer lacks the coordinated snapshot protocol.')
        if (response.status != 200
                or response.getheader('X-CLIProxy-Credential-Protocol') != '1'):
            raise BridgeError('credential_snapshot_pending',
                              'The credential writer could not provide a coherent capture.')
        expected = revision(response.getheader('X-CLIProxy-Credential-Revision'))
        if method == 'revision':
            raw = response.read(4097)
            if len(raw) > 4096:
                raise ValueError()
            value = json.loads(raw)
            if (not isinstance(value, dict)
                    or set(value) != {'format_version', 'revision', 'files_count', 'bytes'}
                    or value['format_version'] != '1' or value['revision'] != expected
                    or type(value['files_count']) is not int
                    or not 0 <= value['files_count'] <= MAX_FILES
                    or type(value['bytes']) is not int or not 0 <= value['bytes'] <= MAX_BYTES):
                raise ValueError()
            return value
        fd, name = tempfile.mkstemp(prefix='.writer-', dir=target.parent)
        temporary = target.parent / os.path.basename(name)
        size = 0
        with os.fdopen(fd, 'wb') as stream:
            for block in iter(lambda: response.read(1024 * 1024), b''):
                size += len(block)
                if size > MAX_ARCHIVE:
                    raise ValueError()
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        result = describe(temporary, capture_id)
        if result['revision'] != expected:
            raise ValueError()
        try:
            os.link(temporary, target)
        except FileExistsError:
            if describe(target, capture_id) != result:
                raise ValueError()
        content.sync_directory(target.parent)
        return result
    except BridgeError:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise BridgeError('credential_snapshot_pending',
                          'The credential writer capture remains unverified.') from None
    finally:
        connection.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
