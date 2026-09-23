"""Client for the protected local managed proxy control socket.

The supervisor generates proxy keys and returns them over its private socket.
This client installs them in the current process environment when an account
route needs them. Neither key enters public AgentBridge account projections.
"""

from hashlib import sha256
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time

from ..errors import BridgeError
from ..models import identifier
from .route import ProxyRoute


_MAX_MESSAGE = 16 * 1024
_MANAGED_NAME = re.compile(r"AGENTBRIDGE_PROXY_(CLIENT|MANAGEMENT)_[A-F0-9]{32}\Z")


def _names(account_id):
    identifier(account_id)
    suffix = sha256(account_id.encode("ascii")).hexdigest()[:32].upper()
    return (f"AGENTBRIDGE_PROXY_CLIENT_{suffix}",
            f"AGENTBRIDGE_PROXY_MANAGEMENT_{suffix}")


def _managed_root(root):
    state_root = Path(root).expanduser().resolve()
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state_root.stat().st_uid != os.getuid():
        raise BridgeError("unsafe_store", "The AgentBridge state directory must belong to this user.")
    managed = state_root / "managed-proxies"
    if managed.is_symlink():
        raise BridgeError("unsafe_store", "The managed proxy directory cannot be a symbolic link.")
    managed.mkdir(mode=0o700, exist_ok=True)
    if managed.stat().st_uid != os.getuid():
        raise BridgeError("unsafe_store", "The managed proxy directory must belong to this user.")
    os.chmod(managed, 0o700)
    return managed


@contextmanager
def _socket_address(directory):
    """Use a directory fd to keep the Unix pathname short without moving it."""
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        yield f"/proc/self/fd/{descriptor}/supervisor.sock"
    finally:
        os.close(descriptor)


class ManagedProxyClient:
    """Provision and reconnect one isolated CLIProxyAPI sidecar per account."""

    def __init__(self, root):
        self.directory = _managed_root(root)
        self.socket_path = self.directory / "supervisor.sock"

    def provision(self, account_id):
        """Create or reconnect a private sidecar and install its key variables."""
        return self._route(account_id, self._request("provision", account_id))

    def ensure(self, account_id, base_url):
        """Reconnect a saved route without changing its account or endpoint."""
        if not isinstance(base_url, str):
            raise BridgeError("invalid_proxy_endpoint", "A managed proxy route needs its saved endpoint.")
        result = self._request("ensure", account_id, base_url=base_url)
        if result.get("proxy_base_url") != base_url:
            raise BridgeError("proxy_binding_changed", "The managed proxy endpoint changed.")
        return self._route(account_id, result)

    def retire(self, account_id):
        """Stop the sidecar while retaining its upstream credential directory."""
        return self._request("retire", account_id)

    def shutdown(self):
        """Explicitly stop this state's sidecars and supervisor (mainly for tests)."""
        return self._request("shutdown", "supervisor")

    @staticmethod
    def is_managed(route_config, account_id=None):
        """Recognize managed key references; pass account_id for exact matching."""
        if not isinstance(route_config, dict):
            return False
        client = route_config.get("key_env")
        management = route_config.get("management_key_env")
        if not isinstance(client, str) or not isinstance(management, str):
            return False
        if account_id is not None:
            try:
                return (client, management) == _names(account_id)
            except BridgeError:
                return False
        return bool(_MANAGED_NAME.fullmatch(client) and
                    _MANAGED_NAME.fullmatch(management) and
                    client.rsplit("_", 1)[-1] == management.rsplit("_", 1)[-1])

    def _route(self, account_id, result):
        keys = ("proxy_base_url", "key_env", "management_key_env",
                "client_key", "management_key")
        if not isinstance(result, dict) or any(not isinstance(result.get(key), str) or
                                               not result[key] for key in keys):
            raise BridgeError("managed_proxy_protocol_error", "The managed proxy returned an invalid route.")
        if not self.is_managed(result, account_id):
            raise BridgeError("managed_proxy_protocol_error", "The managed proxy returned invalid key references.")
        ProxyRoute(account_id, result["proxy_base_url"], result["key_env"])
        os.environ[result["key_env"]] = result["client_key"]
        os.environ[result["management_key_env"]] = result["management_key"]
        return {key: result[key] for key in ("proxy_base_url", "key_env", "management_key_env")}

    def _request(self, action, account_id, **fields):
        if not sys.platform.startswith("linux") or not hasattr(os, "memfd_create"):
            raise BridgeError("unsupported_platform", "Managed proxies require Linux memfd support.")
        identifier(account_id)
        request = {"action": action, "account_id": account_id, **fields}
        payload = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        for attempt in range(2):
            try:
                with _socket_address(self.directory) as address:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                        connection.settimeout(40)
                        connection.connect(address)
                        connection.sendall(payload)
                        with connection.makefile("rb") as stream:
                            line = stream.readline(_MAX_MESSAGE + 1)
                if not line or len(line) > _MAX_MESSAGE or not line.endswith(b"\n"):
                    raise BridgeError("managed_proxy_protocol_error", "The managed proxy returned an invalid response.")
                response = json.loads(line)
                if not isinstance(response, dict):
                    raise ValueError("invalid response")
                if response.get("ok") is True and isinstance(response.get("result"), dict):
                    return response["result"]
                error = response.get("error")
                if (isinstance(error, dict) and isinstance(error.get("code"), str) and
                        isinstance(error.get("message"), str)):
                    raise BridgeError(error["code"], error["message"])
                raise ValueError("invalid response")
            except (ConnectionRefusedError, FileNotFoundError, OSError) as error:
                if attempt == 0 and isinstance(error, (ConnectionRefusedError, FileNotFoundError)):
                    self._start_supervisor()
                    continue
                raise BridgeError("managed_proxy_unavailable", "The managed proxy supervisor is unavailable.") from None
            except (UnicodeError, ValueError):
                raise BridgeError("managed_proxy_protocol_error", "The managed proxy returned an invalid response.") from None
        raise BridgeError("managed_proxy_unavailable", "The managed proxy supervisor is unavailable.")

    def _start_supervisor(self):
        if self.socket_path.exists():
            details = self.socket_path.lstat()
            if not stat.S_ISSOCK(details.st_mode) or details.st_uid != os.getuid():
                raise BridgeError("unsafe_store", "The managed proxy socket is unsafe.")
        source_root = str(Path(__file__).resolve().parents[2])
        environment = {key: value for key, value in os.environ.items() if key in {
            "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "AGENTBRIDGE_CLIPROXY_BIN"}}
        environment["PYTHONPATH"] = source_root
        try:
            subprocess.Popen(
                [sys.executable, "-m", "agentbridge.proxy.supervisor", str(self.directory)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True, env=environment,
                cwd=str(self.directory),
            )
        except OSError:
            raise BridgeError("managed_proxy_unavailable", "The managed proxy supervisor could not start.") from None
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                with _socket_address(self.directory) as address:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                        probe.settimeout(0.2)
                        probe.connect(address)
                return
            except OSError:
                time.sleep(0.05)
        raise BridgeError("managed_proxy_unavailable", "The managed proxy supervisor did not start.")
