"""Stdio client for the GrantBridge proxy OAuth adapter.

GrantBridge coordinates OAuth through CLIProxyAPI's local Management API.
CLIProxyAPI owns the resulting provider credential and token refresh.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import threading
import time

from .errors import BridgeError
from .auth_contract import response_result


class GrantBridgeClient:
    """Drive one local GrantBridge adapter process over JSON-RPC 2.0."""

    def __init__(self, grantbridge_root=None, *, data_dir=None, node=None, adapter=None, timeout=30):
        configured = grantbridge_root or os.environ.get("AGENTBRIDGE_GRANTBRIDGE_ROOT")
        if adapter is None:
            if configured:
                root = Path(configured).expanduser().resolve()
            else:
                candidates = (Path.cwd() / "grantbridge",
                              Path.cwd().parent / "grantbridge",
                              Path(__file__).resolve().parents[3] / "grantbridge")
                root = next((candidate for candidate in candidates
                             if (candidate / "scripts" / "agentbridge-adapter.mjs").is_file()), None)
            if root is None:
                raise BridgeError("grantbridge_unavailable", "GrantBridge is not configured. Pass --grantbridge-root or set AGENTBRIDGE_GRANTBRIDGE_ROOT.")
            adapter = root / "scripts" / "agentbridge-adapter.mjs"
        self.adapter = Path(adapter).expanduser().resolve()
        if not self.adapter.is_file():
            raise BridgeError("grantbridge_unavailable", "The GrantBridge AgentBridge adapter was not found.")
        self.data_dir = Path(data_dir).expanduser().resolve() if data_dir else None
        self.node = node or os.environ.get("AGENTBRIDGE_NODE", "node")
        self.timeout = max(1.0, float(timeout))
        self.process = None
        self._next_id = 0
        self._lock = threading.Lock()
        self._buffer = b''

    def configuration(self):
        """Non-secret references sufficient to reconnect from another process."""
        return {'adapter': str(self.adapter), 'data_dir': str(self.data_dir) if self.data_dir else None,
                'node': self.node}

    def _start(self):
        command = [self.node, str(self.adapter)]
        if self.data_dir:
            command.extend(("--data-dir", str(self.data_dir)))
        env = {name: os.environ[name] for name in (
            "PATH", "HOME", "TMPDIR", "LANG", "GRANTBRIDGE_CODEX", "GRANTBRIDGE_CLAUDE",
            "GRANTBRIDGE_CHROME", "GRANTBRIDGE_CLIENT_NAME",
        ) if os.environ.get(name)}
        env["NO_COLOR"] = "1"
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, env=env, text=False, bufsize=0)
        except OSError as error:
            raise BridgeError("grantbridge_unavailable", "The GrantBridge adapter could not be started.") from error

    def _request(self, method, params=None):
        with self._lock:
            if self.process is None:
                self._start()
            process = self.process
            self._next_id += 1
            request_id = self._next_id
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            try:
                process.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                raise BridgeError("grantbridge_failed", "The GrantBridge adapter connection ended.") from None
            selector = selectors.DefaultSelector()
            try:
                selector.register(process.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + self.timeout
                while True:
                    events = selector.select(max(0, deadline - time.monotonic()))
                    if not events:
                        raise BridgeError("grantbridge_timeout", "GrantBridge did not answer in time.")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise BridgeError("grantbridge_failed", "The GrantBridge adapter exited unexpectedly.")
                    self._buffer += chunk
                    if len(self._buffer) > 1024 * 1024:
                        raise BridgeError('provider_protocol_error', 'GrantBridge response exceeded the size limit.')
                    while b'\n' in self._buffer:
                        line, self._buffer = self._buffer.split(b'\n', 1)
                        try:
                            response = json.loads(line)
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if not isinstance(response, dict) or response.get('id') != request_id:
                            continue
                        return response_result(response)
                    if time.monotonic() >= deadline:
                        raise BridgeError('grantbridge_timeout', 'GrantBridge did not answer in time.')
            finally:
                selector.close()

    @staticmethod
    def _proxy_key(name):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', name):
            raise BridgeError('invalid_environment', 'Use an environment variable name for the management key.')
        value = os.environ.get(name)
        if not value or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise BridgeError('credential_unavailable', 'The local proxy management key is unavailable.')
        return value

    def proxy_start(self, provider, base_url, management_key_env):
        """Ask GrantBridge to start OAuth in a dedicated local proxy."""
        return self._request('auth.proxy_start', {'provider': provider, 'base_url': base_url,
            'management_key': self._proxy_key(management_key_env)})

    def proxy_status(self, state, provider, base_url, management_key_env):
        return self._request('auth.proxy_status', {'state': state, 'provider': provider,
            'base_url': base_url, 'management_key': self._proxy_key(management_key_env)})

    def proxy_cancel(self, state, provider, base_url, management_key_env):
        return self._request('auth.proxy_cancel', {'state': state, 'provider': provider,
            'base_url': base_url, 'management_key': self._proxy_key(management_key_env)})

    def proxy_callback(self, state, provider, base_url, management_key_env, redirect_url):
        """Deliver one-use OAuth code over stdio; never persist it in AgentBridge."""
        return self._request('auth.proxy_callback', {
            'state': state, 'provider': provider, 'base_url': base_url,
            'management_key': self._proxy_key(management_key_env),
            'redirect_url': redirect_url,
        })

    def close(self):
        process = self.process
        if process is None:
            return
        timeout = self.timeout
        self.timeout = min(timeout, 2)
        try:
            self._request("auth.close")
        except (BridgeError, OSError):
            pass
        finally:
            self.timeout = timeout
            self.process = None
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=3)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
