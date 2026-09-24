"""Prepare reviewed CLIProxyAPI release assets and update their immutable pin."""

import argparse
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import re
import tarfile
from urllib.request import urlopen
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / 'src/agentbridge/bundle/lock.json'
RELEASE_ROOT = 'https://github.com/router-for-me/CLIProxyAPI/releases/download'
ARCHIVES = {
    'linux_x86_64': ('amd64', 62),
    'linux_aarch64': ('aarch64', 183),
}
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_CHECKSUM_BYTES = 64 * 1024
MAX_MEMBER_BYTES = 160 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
VERSION = re.compile(r'v?([0-9]+\.[0-9]+\.[0-9]+)\Z')
CHECKSUM = re.compile(r'([0-9a-f]{64})  \*?([^/\s]+)\Z')


class UpdateError(ValueError):
    """A candidate cannot be pinned without review or integrity evidence."""


def _version(value):
    matched = VERSION.fullmatch(value)
    if matched is None:
        raise UpdateError('Choose an exact CLIProxyAPI vMAJOR.MINOR.PATCH release.')
    return matched.group(1)


def _read_url(url, limit, opener):
    with opener(url, timeout=30) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise UpdateError('The upstream checksum document exceeds its size limit.')
    return body


def _checksums(body, expected):
    try:
        lines = body.decode('ascii').splitlines()
    except UnicodeDecodeError:
        raise UpdateError('The upstream checksum document is invalid.') from None
    found = {}
    for line in lines:
        matched = CHECKSUM.fullmatch(line)
        if matched is None:
            raise UpdateError('The upstream checksum document is malformed.')
        digest, name = matched.groups()
        if name in expected:
            if name in found:
                raise UpdateError('The upstream checksum document has duplicate assets.')
            found[name] = digest
    if set(found) != expected:
        raise UpdateError('The upstream checksum document is missing a required asset.')
    return found


def _download(url, path, expected_sha256, opener):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size <= MAX_ARCHIVE_BYTES:
        with path.open('rb') as existing:
            if file_digest(existing, 'sha256').hexdigest() == expected_sha256:
                return
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    digest, size = sha256(), 0
    try:
        with opener(url, timeout=30) as response, temporary.open('xb') as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise UpdateError('The CLIProxyAPI release archive exceeds its size limit.')
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected_sha256:
            raise UpdateError('The CLIProxyAPI release archive differs from its checksum.')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _inspect_archive(path, machine):
    try:
        names, size = set(), 0
        header, license_text = b'', b''
        with tarfile.open(path, 'r|gz') as archive:
            for member in archive:
                size += member.size
                if (len(names) >= 16 or member.name in names
                        or not member.isfile() or member.name != Path(member.name).name
                        or member.size < 0 or member.size > MAX_MEMBER_BYTES
                        or size > MAX_UNPACKED_BYTES):
                    raise UpdateError('The CLIProxyAPI release archive has unsafe members.')
                names.add(member.name)
                if member.name == 'cli-proxy-api':
                    binary = archive.extractfile(member)
                    header = binary.read(20) if binary is not None else b''
                elif member.name == 'LICENSE':
                    license_file = archive.extractfile(member)
                    license_text = license_file.read(16 * 1024 + 1) if license_file else b''
        if not {'cli-proxy-api', 'LICENSE'} <= names:
            raise UpdateError('The CLIProxyAPI release archive is missing required members.')
        if (len(header) != 20 or header[:6] != b'\x7fELF\x02\x01'
                or int.from_bytes(header[18:20], 'little') != machine):
            raise UpdateError('The CLIProxyAPI release binary has the wrong architecture.')
        if not 0 < len(license_text) <= 16 * 1024 or b'MIT License' not in license_text:
            raise UpdateError('The CLIProxyAPI release license is missing or invalid.')
    except (OSError, EOFError, tarfile.TarError, ValueError) as error:
        if isinstance(error, UpdateError):
            raise
        raise UpdateError('The CLIProxyAPI release archive is invalid.') from None


def update_release(version, output_dir, lock_path=LOCK_PATH, *, write=False, opener=urlopen):
    """Download both architectures and optionally pin the verified candidate."""
    version = _version(version)
    base = f'{RELEASE_ROOT}/v{version}'
    names = {platform: f'CLIProxyAPI_{version}_linux_{arch}_no-plugin.tar.gz'
             for platform, (arch, _) in ARCHIVES.items()}
    checksums = _checksums(_read_url(f'{base}/checksums.txt', MAX_CHECKSUM_BYTES, opener),
                           set(names.values()))
    assets = {}
    for platform, name in names.items():
        url = f'{base}/{name}'
        digest = checksums[name]
        target = Path(output_dir) / f'v{version}' / name
        _download(url, target, digest, opener)
        _inspect_archive(target, ARCHIVES[platform][1])
        assets[platform] = {'url': url, 'sha256': digest}
    value = {'version': version, 'assets': assets}
    if write:
        lock_path = Path(lock_path)
        existing = json.loads(lock_path.read_text()) if lock_path.exists() else {}
        if not isinstance(existing, dict):
            raise UpdateError('The runtime lock document is invalid.')
        existing['cli_proxy_api'] = value
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = lock_path.with_name(f'.{lock_path.name}.{uuid4().hex}.tmp')
        try:
            temporary.write_text(json.dumps(existing, indent=2, sort_keys=True) + '\n')
            os.replace(temporary, lock_path)
        finally:
            temporary.unlink(missing_ok=True)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True, help='Exact upstream release, such as v7.3.16')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist/cli_proxy_api')
    parser.add_argument('--write', action='store_true', help='Update the reviewed runtime lock')
    arguments = parser.parse_args(argv)
    try:
        value = update_release(arguments.version, arguments.output_dir, write=arguments.write)
    except (OSError, UpdateError, json.JSONDecodeError) as error:
        parser.exit(1, f'CLIProxyAPI candidate rejected: {error}\n')
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
