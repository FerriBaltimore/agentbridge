"""Bounded native JSON-lines channel. No shell, stderr capture or credential log."""
import json
import os
import select
import shutil
import subprocess
import time

from .errors import BridgeError
from .native_sandbox import wrap


class ProviderChannel:
    def __init__(self, command, *, cwd, env, inputs_only=False,
                 workspace_write=False, mcp_enabled=False, full_access=False,
                 selected_context=False):
        if not command or shutil.which(command[0], path=env.get('PATH', os.defpath)) is None:
            raise BridgeError('provider_unavailable', 'The native provider could not be started.',
                              phase='launch', outcome='not_started')
        command = wrap(command, inputs_only=inputs_only,
                       workspace_write=workspace_write, mcp_enabled=mcp_enabled,
                       full_access=full_access, selected_context=selected_context)
        try:
            self.process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            raise BridgeError('provider_unavailable', 'The native provider could not be started.',
                              phase='launch', outcome='not_started') from None
        self.buffer = b''
        os.set_blocking(self.process.stdout.fileno(), False)

    def send(self, value):
        try:
            self.process.stdin.write((json.dumps(value) + '\n').encode())
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise BridgeError('provider_connection_lost', 'The native provider connection closed.',
                              phase='execution', outcome='unknown') from None

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
                raise BridgeError('provider_protocol_error', 'Invalid native provider message.',
                                  phase='execution', outcome='unknown')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError('provider_timeout', 'The native provider did not answer in time.',
                                  phase='execution', outcome='unknown')
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                if self.buffer:
                    raise BridgeError('provider_protocol_error', 'The native provider message was truncated.',
                                      phase='execution', outcome='unknown')
                raise BridgeError('provider_connection_lost', 'The native provider ended its stream.',
                                  phase='execution', outcome='unknown')
            self.buffer += chunk
            if len(self.buffer) > 8 * 1024 * 1024:
                raise BridgeError('provider_protocol_error', 'The native provider message is too large.',
                                  phase='execution', outcome='unknown')

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
