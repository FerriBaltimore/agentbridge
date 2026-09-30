"""Loopback writer protocol fixture; no provider calls or inference acceptance claims."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tarfile
from threading import RLock

lock = RLock()


def inventory():
    files, contents = [], {}
    for path in sorted(root.rglob('*')):
        if path.is_file() and not path.name.startswith('.oauth'):
            data = path.read_bytes()
            name = path.relative_to(root).as_posix()
            contents[name] = data
            files.append({'path': name, 'sha256': hashlib.sha256(data).hexdigest(),
                          'bytes': len(data)})
    revision = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {'format_version': '1', 'revision': revision, 'files': files}, contents


class Handler(BaseHTTPRequestHandler):
    def send(self, value, *, revision=None):
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        if revision:
            self.send_header('X-CLIProxy-Credential-Protocol', '1')
            self.send_header('X-CLIProxy-Credential-Revision', revision)
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if self.headers.get('Authorization') != 'Bearer ' + config['remote-management']['secret-key']:
            self.send_error(401)
            return False
        return True

    def handle_get(self):
        if not self.authorized():
            return
        if self.path == '/v0/management/config':
            return self.send({})
        if self.path not in {'/v0/management/auth-files/snapshot',
                             '/v0/management/auth-files/revision'}:
            return self.send_error(404)
        with lock:
            manifest, contents = inventory()
        if self.path.endswith('revision'):
            return self.send({'format_version': '1', 'revision': manifest['revision'],
                              'files_count': len(contents),
                              'bytes': sum(map(len, contents.values()))},
                             revision=manifest['revision'])
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as archive:
            for name, data in [('credential-files.json', json.dumps(manifest).encode()),
                               *contents.items()]:
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(data), 0o600
                archive.addfile(item, io.BytesIO(data))
        self.send(output.getvalue(), revision=manifest['revision'])

    def handle_post(self):
        if not self.authorized():
            return
        if self.path != '/fixture/rotate':
            return self.send_error(404)
        value = self.rfile.read(int(self.headers['Content-Length']))
        with lock:
            (root / 'credential.json').write_bytes(value)
        self.send({'persisted': True})

    def log_message(self, *_):
        pass


setattr(Handler, 'do_GET', Handler.handle_get)
setattr(Handler, 'do_POST', Handler.handle_post)
if __name__ == '__main__':
    with open(sys.argv[sys.argv.index('-config') + 1]) as stream:
        config = json.load(stream)
    root = Path(config['auth-dir'])
    ThreadingHTTPServer(('127.0.0.1', config['port']), Handler).serve_forever()
