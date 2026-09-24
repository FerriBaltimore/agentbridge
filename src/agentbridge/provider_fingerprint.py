"""Bounded offline provider evidence. Fingerprints never grant compatibility."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from .catalog_process import read_output
from .errors import BridgeError


MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_FILES = 512
MAX_NODES = 200000
VERSION = r'[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}'


def _invalid():
    return BridgeError('provider_protocol_error', 'Offline provider evidence is invalid or exceeds its limits.')


def canonical_hash(value):
    """Hash JSON values without key-order differences or lossy coercion."""
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > MAX_NODES or depth > 100:
            raise _invalid()
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise _invalid()
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise _invalid()
    try:
        data = json.dumps(value, sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, OverflowError, UnicodeError):
        raise _invalid() from None
    if len(data) > MAX_TOTAL_BYTES:
        raise _invalid()
    return hashlib.sha256(data).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(_invalid()))
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid() from None


def _read(path):
    """Do not follow a final symlink or read an unbounded changing file."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise _invalid()
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise _invalid()
        return data
    except OSError:
        raise _invalid() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _environment(home):
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR') if key in os.environ}
    env.update({'HOME': str(home), 'CODEX_HOME': str(home / 'codex'),
                'CLAUDE_CONFIG_DIR': str(home / 'claude'), 'TMPDIR': str(home),
                'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_DATA_HOME': str(home / 'data'),
                'XDG_CACHE_HOME': str(home / 'cache'), 'XDG_STATE_HOME': str(home / 'state'),
                'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TERM': 'dumb', 'COLUMNS': '120',
                'DISABLE_AUTOUPDATER': '1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'})
    return env


def _run(command, home, *, timeout=5, max_bytes=4096):
    try:
        return read_output(command, env=_environment(home), cwd=str(home),
                           timeout=timeout, max_bytes=max_bytes)
    except BridgeError as error:
        raise BridgeError(error.code, 'Offline provider inspection failed.') from None


def _version(data, engine):
    try:
        output = data.decode('utf-8').strip()
    except UnicodeError:
        return None
    expression = {'codex': r'codex-cli (' + VERSION + ')',
                  'claude': '(' + VERSION + r') \(Claude Code\)'}[engine]
    match = re.fullmatch(expression, output)
    return match[1] if match else None


def _schemas(root):
    if root.is_symlink():
        raise _invalid()
    pending, files, total, visited = [root], [], 0, 0
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > MAX_FILES + 32 or entry.is_symlink():
                        raise _invalid()
                    if entry.is_dir(follow_symlinks=False):
                        if len(Path(entry.path).relative_to(root).parts) > 4:
                            raise _invalid()
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and entry.name.endswith('.json'):
                        content = _read(entry.path)
                        total += len(content)
                        if total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
                            raise _invalid()
                        schema = _json(content)
                        if not isinstance(schema, dict):
                            raise _invalid()
                        files.append({'path': str(Path(entry.path).relative_to(root)), 'schema': schema})
                    else:
                        raise _invalid()
        except OSError:
            raise _invalid() from None
    if not files:
        raise _invalid()
    return sorted(files, key=lambda item: item['path'])


def _methods(files):
    names = set()
    for item in files:
        if item['path'] not in {'ClientRequest.json', 'ServerRequest.json', 'ServerNotification.json'}:
            continue
        variants = item['schema'].get('oneOf')
        if not isinstance(variants, list):
            raise _invalid()
        for variant in variants:
            properties = variant.get('properties') if isinstance(variant, dict) else None
            method = properties.get('method') if isinstance(properties, dict) else None
            choices = method.get('enum') if isinstance(method, dict) else None
            if not isinstance(choices, list) or any(not _name(name) for name in choices):
                raise _invalid()
            names.update(choices)
    if not names:
        raise _invalid()
    return sorted(names)


def _name(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and all(32 <= ord(c) < 127 for c in value)


def _native(engine, state_root=None):
    if engine == 'codex' and state_root is not None:
        from .bundle.runtime import resolve_binary
        executable = str(resolve_binary('codex', state_root))
    else:
        executable = shutil.which(engine)
    if executable is None:
        raise BridgeError('provider_unavailable', 'The provider executable is unavailable.')
    with tempfile.TemporaryDirectory(prefix='agentbridge-contract-') as directory:
        home = Path(directory)
        version_output = _run([executable, '--version'], home)
        version = _version(version_output, engine)
        limitations = ['offline_evidence_only', 'no_account_or_backend_acceptance', 'exact_binding_review_required']
        if version is None:
            limitations.append('unrecognized_version_output')
        if engine == 'codex':
            root = home / 'schemas'
            _run([executable, 'app-server', 'generate-json-schema', '--out', str(root)],
                 home, timeout=20, max_bytes=16384)
            files = _schemas(root)
            digest, names, kind = canonical_hash(files), _methods(files), 'codex_json_schema_default_v1'
            limitations.extend(['schema_presence_does_not_enable_operation', 'generated_disk_usage_not_quota_enforced'])
        else:
            output = _run([executable, '--help'], home, max_bytes=128 * 1024)
            try:
                help_text = '\n'.join(line.rstrip() for line in output.decode('utf-8').splitlines())
            except UnicodeError:
                raise _invalid() from None
            names = sorted(set(re.findall(r'^\s*(?:-[a-zA-Z],\s*)?(--[a-zA-Z][a-zA-Z0-9-]*)(?=[\s,=]|$)', help_text, re.M)))
            if not names:
                raise _invalid()
            digest, kind = canonical_hash({'help': help_text}), 'cli_help_observation_v1'
            limitations.extend(['help_is_not_protocol_schema', 'cannot_certify_structural_equivalence'])
        if _run([executable, '--version'], home) != version_output:
            raise BridgeError('provider_contract_changed', 'The provider version changed during offline inspection.',
                              details={'engine': engine, 'version': version, 'component': engine + '-cli'})
        return {'component': engine + '-cli', 'version': version, 'evidence_kind': kind,
                'structural_hash': digest, 'surface_names': names, 'limitations': limitations}


def inspect_surface(engine, state_root=None):
    """Inspect the managed Codex or an installed legacy CLI without accounts.

    Hash equality is evidence for review, never automatic version acceptance.
    Claude's structural_hash intentionally contains an observation-only help
    digest; its evidence_kind prohibits treating it as structural equivalence.
    """
    if engine not in ('codex', 'claude'):
        raise BridgeError('invalid_engine', 'Unknown provider for offline inspection.')
    return _native(engine, state_root)
