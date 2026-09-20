"""Bounded native JSON-lines channel. No shell, stderr capture or credential log."""
import json
import os
import select
import subprocess
import time

from .errors import BridgeError


class ProviderChannel:
    def __init__(self, command, *, cwd, env):
        try:
            self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            raise BridgeError('provider_unavailable', 'The native provider could not be started.') from None
        self.buffer = b''
        os.set_blocking(self.process.stdout.fileno(), False)

    def send(self, value):
        try:
            self.process.stdin.write((json.dumps(value) + '\n').encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise BridgeError('provider_unavailable', 'The native provider connection closed.') from None

    def receive(self, timeout=30):
        deadline = time.monotonic() + timeout
        while True:
            if b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        return value
                except (ValueError, UnicodeDecodeError):
                    pass
                raise BridgeError('provider_protocol_error', 'Invalid native provider message.')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError('provider_timeout', 'The native provider did not answer in time.')
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise BridgeError('provider_unavailable', 'The native provider ended its stream.')
            self.buffer += chunk
            if len(self.buffer) > 8 * 1024 * 1024:
                raise BridgeError('provider_protocol_error', 'The native provider message is too large.')

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
