"""Provider probes used by the account observation service.

Probes return bounded observations and never persist credential values.
"""
from datetime import datetime, timezone
import json
import os
import select
import signal
import subprocess
import time

from .errors import BridgeError
from .security import base_environment


def stamp(value=None):
    return datetime.fromtimestamp(value or time.time(), timezone.utc).isoformat(timespec="seconds")


def safe_text(value, maximum=320):
    return value if isinstance(value, str) and 0 < len(value) <= maximum else None


def safe_identity(provider_account):
    value = provider_account if isinstance(provider_account, dict) else {}
    result = {}
    for key in ("type", "email", "name", "planType", "plan_type", "account_id", "accountId"):
        item = value.get(key)
        if safe_text(item):
            result[key] = item
    return result


class CodexAppServerProbe:
    """Read Codex account facts without running a model turn."""

    source = "codex_app_server"

    def __init__(self, account, *, timeout=30):
        self.account = account
        self.timeout = timeout
        self.process = None
        self.next_id = 0

    def _command(self):
        return list(self.account.command or ("codex",)) + ["app-server", "--stdio"]

    def _start(self):
        env = base_environment()
        env["CODEX_HOME"] = self.account.home
        for name in (*self.account.env_names, *((self.account.key_env,) if self.account.key_env else ())):
            if name not in os.environ:
                raise BridgeError("credential_unavailable", f"Required environment variable {name} is unavailable.")
            env[name] = os.environ[name]
        try:
            self.process = subprocess.Popen(
                self._command(), cwd=self.account.home, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
                start_new_session=True,
            )
        except OSError as error:
            raise BridgeError("provider_unavailable", "The provider process could not be started.") from error

    def _rpc(self, method, params=None):
        if not self.process:
            self._start()
        self.next_id += 1
        request = {"id": self.next_id, "method": method, "params": params or {}}
        try:
            self.process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                ready, _, _ = select.select([self.process.stdout], [], [], max(0, deadline - time.monotonic()))
                if not ready:
                    break
                line = self.process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if message.get("id") != request["id"]:
                    continue
                if isinstance(message.get("error"), dict):
                    raise BridgeError("provider_failed", "The provider rejected the account query.")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise BridgeError("provider_protocol_error", "The provider returned an invalid account response.")
                return result
        except (BrokenPipeError, OSError):
            raise BridgeError("provider_failed", "The provider connection ended during the account query.") from None
        raise BridgeError("provider_timeout", "The provider did not answer the account query in time.")

    def read(self, *, include_usage=False):
        try:
            self._rpc("initialize", {"clientInfo": {"name": "agentbridge", "title": "AgentBridge", "version": "0.1"}})
            self._notify("initialized", {})
            result = self._rpc("account/read", {"refreshToken": False})
            provider_account = result.get("account")
            identity = safe_identity(provider_account)
            if provider_account is None:
                status = "authentication_required" if result.get("requiresOpenaiAuth") else "unauthenticated"
            elif isinstance(provider_account, dict):
                status = "authenticated"
            else:
                status = "provider_protocol_error"
            data = {"status": status, "identity": identity,
                    "requires_openai_auth": bool(result.get("requiresOpenaiAuth"))}
            if include_usage and status == "authenticated":
                for key, method in (("quota", "account/rateLimits/read"), ("account_usage", "account/usage/read")):
                    try:
                        data[key] = self._rpc(method)
                    except BridgeError as error:
                        data[f"{key}_reason"] = error.code
            return data
        finally:
            self.close()

    def list_models(self):
        """Read Codex's model catalog without starting a model turn."""
        try:
            self._rpc("initialize", {"clientInfo": {"name": "agentbridge", "title": "AgentBridge", "version": "0.1"}})
            self._notify("initialized", {})
            result = self._rpc("model/list", {})
            models = result.get("data", result.get("models", []))
            if not isinstance(models, list):
                raise BridgeError("provider_protocol_error", "The provider returned an invalid model catalog.")
            return models
        finally:
            self.close()

    def _notify(self, method, params):
        if self.process:
            self.process.stdin.write(json.dumps({"method": method, "params": params}, separators=(",", ":")) + "\n")
            self.process.stdin.flush()

    def close(self):
        process, self.process = self.process, None
        if not process:
            return
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            for signal_name, wait in ((signal.SIGTERM, 2), (signal.SIGKILL, 0)):
                try:
                    os.killpg(process.pid, signal_name)
                except (OSError, ProcessLookupError):
                    pass
                if wait:
                    try:
                        process.wait(timeout=wait)
                    except (OSError, subprocess.TimeoutExpired):
                        continue
                break
