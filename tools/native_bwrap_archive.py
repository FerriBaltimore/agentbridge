"""Reproduce the reviewed two-member native launcher archive and retained license notices."""

from __future__ import annotations

import argparse
import gzip
from hashlib import sha256
import io
import json
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'src/agentbridge/bundle'
NOTICE = (
    'Bubblewrap copyright (C) 2016 Alexander Larsson; SPDX LGPL-2.0-or-later.\n'
    'Built from bubblewrap v0.11.1 commit 124c4cdf4321f63ef17a1cb0ce8f9dd45bd7adbe.\n'
    'Source: https://github.com/containers/bubblewrap\n'
    'Patch and reproducible static build recipe: tools/native_bwrap.patch and tools/native_bwrap_build.sh\n'
    'Statically linked libcap 2.75 (Ubuntu 1:2.75-10ubuntu2), glibc 2.43 (2.43-2ubuntu2.4).\n'
    'Dependency sources: https://launchpad.net/ubuntu/+source/libcap2/1:2.75-10ubuntu2\n'
    'https://launchpad.net/ubuntu/+source/glibc/2.43-2ubuntu2.4\n'
    'Bubblewrap and dependency license terms are retained below; no SDK license is selected here.\n'
)


def license_bytes(upstream, libcap_root, libc_copyright, lgpl):
    sections = [
        ('Native bubblewrap 0.11.1.roproc1', NOTICE),
        ('Bubblewrap license', (upstream / 'COPYING').read_text()),
        ('libcap copyright and license notices',
         (libcap_root / 'usr/share/doc/libcap2/copyright').read_text()),
        ('glibc copyright and license notices', libc_copyright.read_text()),
        ('GNU Lesser General Public License version 2.1', lgpl.read_text()),
    ]
    return '\n\n'.join(title + '\n' + '=' * len(title) + '\n' + body
                       for title, body in sections).encode()


def archive_bytes(binary, license_data):
    output = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0,
                       compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for name, data, mode in [('bin/bwrap', binary, 0o555), ('LICENSE', license_data, 0o444)]:
                entry = tarfile.TarInfo(name)
                entry.size, entry.mode = len(data), mode
                entry.uid = entry.gid = entry.mtime = 0
                entry.uname = entry.gname = ''
                archive.addfile(entry, io.BytesIO(data))
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--upstream', type=Path, required=True)
    parser.add_argument('--libcap-root', type=Path, required=True)
    parser.add_argument('--libc-copyright', type=Path,
                        default=Path('/usr/share/doc/libc6/copyright'))
    parser.add_argument('--lgpl', type=Path, default=Path('/usr/share/common-licenses/LGPL-2.1'))
    parser.add_argument('--output', type=Path, required=True)
    arguments = parser.parse_args()
    asset = json.loads((PACKAGE / 'lock.json').read_text())['native_bwrap']['assets']['linux_x86_64']
    binary = arguments.binary.read_bytes()
    if sha256(binary).hexdigest() != asset['binary_sha256']:
        raise ValueError('The rebuilt binary differs from the reviewed native launcher.')
    licenses = license_bytes(arguments.upstream, arguments.libcap_root,
                             arguments.libc_copyright, arguments.lgpl)
    data = archive_bytes(binary, licenses)
    if sha256(data).hexdigest() != asset['sha256']:
        raise ValueError('The rebuilt archive or license notices differ from the reviewed asset.')
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_bytes(data)
    print(f'{sha256(data).hexdigest()}  {arguments.output}')


if __name__ == '__main__':
    main()
