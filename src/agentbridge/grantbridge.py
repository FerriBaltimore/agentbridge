"""Minimal stdio client for the optional GrantBridge authentication adapter.

The client transports account IDs and safe authentication projections only. GrantBridge
keeps provider credentials in its own vault and native profile directories.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import threading
import time

from .errors import BridgeError


class GrantBridgeClient:
    """Drive one local GrantBridge adapter process over JSON-RPC 2.0."""

    def __init__(self, grantbridge_root=None, *, data_dir=None, node=None, adapter=None, timeout=30):
        configured = grantbridge_root or os.environ.get("AGENTBRIDGE_GRANTBRIDGE_ROOT")
        if adapter is None:
            if configured:
                root = Path(configured).expanduser().resolve()
            else:
                candidates = [Path(__file__).resolve().parents[3] / "grantbridge"]
                root = next((candidate for candidate in candidates if candidate.is_dir()), None)
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
                        if isinstance(response.get('error'), dict):
                            error = response['error']
                            code = ((error.get('data') or {}).get('code') or 'grantbridge_failed')
                            raise BridgeError(code, 'GrantBridge did not complete the operation.')
                        return response.get('result')
                    if time.monotonic() >= deadline:
                        raise BridgeError('grantbridge_timeout', 'GrantBridge did not answer in time.')
            finally:
                selector.close()

    def start(self, *, owner, engine, mode="browser", browser="same_host", request_key=None, auto_check=False):
        return self._request("auth.start", {"owner": owner, "engine": engine, "mode": mode,
                                             "browser": browser, "request_key": request_key,
                                             "auto_check": auto_check})

    def get(self, attempt_id, owner):
        return self._request("auth.get", {"attempt_id": attempt_id, "owner": owner})

    def find(self, request_key, owner):
        return self._request('auth.find', {'request_key': request_key, 'owner': owner})

    def cancel(self, attempt_id, owner):
        return self._request("auth.cancel", {"attempt_id": attempt_id, "owner": owner})

    def check(self, attempt_id, owner, *, inference=False):
        return self._request("auth.check", {"attempt_id": attempt_id, "owner": owner, 'inference': inference})

    def activate(self, attempt_id, owner):
        return self._request("auth.activate", {"attempt_id": attempt_id, "owner": owner})

    def credentials(self, attempt_id, owner):
        """Private execution channel. Never return this result through public RPC."""
        return self._request('auth.credentials', {'attempt_id': attempt_id, 'owner': owner})

    def submit_code(self, attempt_id, owner, code):
        return self._request("auth.submit_code", {"attempt_id": attempt_id, "owner": owner, "code": code})

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
