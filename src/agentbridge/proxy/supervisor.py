"""Detached same-user supervisor for isolated local CLIProxyAPI sidecars.

Only endpoint and process metadata touch disk. Proxy keys live in this process,
its protected Unix socket responses and the sidecar's anonymous memfd config.
CLIProxyAPI owns each persistent OAuth credential directory.
"""

import fcntl
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time

from ..errors import BridgeError
from ..bundle import resolve_binary
from ..models import identifier
from .managed import _managed_root, _names, _socket_address
from .process_lifecycle import _pid_start, _same_process, _stop_record
from .route import ProxyRoute
from .stop_marker import (clear_stop_marker, read_stop_marker, sync_directory,
                          write_stop_marker)
from .supervisor_auth import create_auth
from .supervisor_protocol import _serve_connection
from .credential_barrier import require_supervisor, supervisor_hold


READY_SECONDS = 15


def _binary(state_root=None):
    configured = os.environ.get('AGENTBRIDGE_CLIPROXY_BIN')
    if not configured:
        if state_root is None:
            raise BridgeError('proxy_binary_unavailable',
                              'The managed CLIProxyAPI runtime needs an AgentBridge state root.')
        return resolve_binary('cli_proxy_api', state_root)
    if not Path(configured).is_absolute():
        raise BridgeError('proxy_binary_unavailable',
                          'Set AGENTBRIDGE_CLIPROXY_BIN to an absolute CLIProxyAPI executable path.')
    path = Path(configured).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise BridgeError('proxy_binary_unavailable',
                          'The configured CLIProxyAPI executable is unavailable.')
    return path


def _port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _account_dir(directory, account_id):
    identifier(account_id)
    accounts = directory / "accounts"
    if accounts.is_symlink():
        raise BridgeError("unsafe_store", "The managed account directory cannot be a symbolic link.")
    accounts.mkdir(mode=0o700, exist_ok=True)
    os.chmod(accounts, 0o700)
    target = accounts / account_id
    if target.is_symlink():
        raise BridgeError("unsafe_store", "A managed proxy account cannot be a symbolic link.")
    target.mkdir(mode=0o700, exist_ok=True)
    os.chmod(target, 0o700)
    auth = target / "auth"
    if auth.is_symlink():
        raise BridgeError("unsafe_store", "A managed proxy auth directory cannot be a symbolic link.")
    auth.mkdir(mode=0o700, exist_ok=True)
    os.chmod(auth, 0o700)
    return target


def _read_record(account_dir):
    path = account_dir / "route.json"
    if not path.exists():
        return None
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise BridgeError("unsafe_store", "The managed proxy route record is unsafe.")
    try:
        value = json.loads(path.read_text())
        if (not isinstance(value, dict) or value.get("version") != 1 or
                not isinstance(value.get("port"), int) or not 1 <= value["port"] <= 65535):
            raise ValueError("invalid record")
        return value
    except (OSError, UnicodeError, ValueError):
        raise BridgeError("managed_proxy_state_invalid", "The managed proxy route record is invalid.") from None


def _write_record(account_dir, record):
    target = account_dir / "route.json"
    temporary = account_dir / f".route-{secrets.token_hex(8)}"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(record, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _upstream_proxy_url(environment=os.environ):
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
                "HTTP_PROXY", "http_proxy"):
        value = environment.get(key, "").strip()
        if value:
            return value
    return None


def _proxy_config(port, auth_dir, client_key, management_key, environment=os.environ):
    config = {"host": "127.0.0.1", "port": port, "auth-dir": str(auth_dir),
              "api-keys": [client_key], "request-retry": 0,
              "remote-management": {"allow-remote": False,
                                    "secret-key": management_key,
                                    "disable-control-panel": True,
                                    "disable-auto-update-panel": True},
              "plugins": {"enabled": False}, "discovery": {"enabled": False},
              "pprof": {"enable": False}, "logging-to-file": False}
    proxy_url = _upstream_proxy_url(environment)
    if proxy_url:
        # CLIProxyAPI's uTLS transport for Codex ignores environment proxies, so a
        # host proxy (for example a sandbox egress proxy) must be configured explicitly.
        config["proxy-url"] = proxy_url
    return config


def _ready(port, key, process):
    deadline = time.monotonic() + READY_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
        try:
            connection.request("GET", "/v0/management/config",
                               headers={"Authorization": f"Bearer {key}",
                                        "Accept": "application/json"})
            response = connection.getresponse()
            body = response.read(1024 * 1024 + 1)
            if response.status == 200 and len(body) <= 1024 * 1024:
                value = json.loads(body)
                if isinstance(value, dict):
                    return True
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(0.1)
    return False


def _recover(record, account_dir, account_id):
    """Reattach to a live sidecar using its inherited anonymous key lease."""
    if not _same_process(record, account_dir):
        return None
    lease_fd = record.get("lease_fd")
    if not isinstance(lease_fd, int) or lease_fd < 3:
        return None
    path = Path(f"/proc/{record['pid']}/fd/{lease_fd}")
    try:
        if "memfd:agentbridge-proxy-lease" not in os.readlink(path):
            return None
        data = path.read_bytes()
        if len(data) > 4096:
            return None
        lease = json.loads(data)
        if (not isinstance(lease, dict) or lease.get("account_id") != account_id or
                lease.get("port") != record["port"] or
                not all(isinstance(lease.get(key), str) and 20 <= len(lease[key]) <= 256
                        for key in ("client_key", "management_key"))):
            return None
        connection = http.client.HTTPConnection("127.0.0.1", record["port"], timeout=1)
        try:
            connection.request("GET", "/v0/management/config",
                               headers={"Authorization": f"Bearer {lease['management_key']}"})
            response = connection.getresponse()
            response.read(1024 * 1024 + 1)
            if response.status != 200:
                return None
        finally:
            connection.close()
        return {"record": record, "process": None,
                "client_key": lease["client_key"],
                "management_key": lease["management_key"]}
    except (OSError, ValueError, KeyError, http.client.HTTPException):
        return None


def _proxy_environment():
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE",
               "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
               "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def _launch(binary, account_dir, account_id, port):
    clear_stop_marker(account_dir)
    client_key = secrets.token_urlsafe(48)
    management_key = secrets.token_urlsafe(48)
    content = json.dumps(_proxy_config(port, account_dir / "auth", client_key,
                                       management_key), separators=(",", ":")).encode()
    descriptor = os.memfd_create("agentbridge-proxy-config", os.MFD_CLOEXEC)
    lease_fd = os.memfd_create("agentbridge-proxy-lease",
                               os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.write(descriptor, content)
        os.lseek(descriptor, 0, os.SEEK_SET)
        lease = json.dumps({"account_id": account_id, "port": port,
                            "client_key": client_key,
                            "management_key": management_key},
                           separators=(",", ":")).encode()
        os.write(lease_fd, lease)
        os.lseek(lease_fd, 0, os.SEEK_SET)
        fcntl.fcntl(lease_fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE |
                    fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        try:
            process = subprocess.Popen(
                [str(binary), "-config", f"/proc/self/fd/{descriptor}"],
                pass_fds=(descriptor, lease_fd), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, close_fds=True, cwd=account_dir,
                env={**_proxy_environment(), 'WRITABLE_PATH': str(account_dir)})
        except OSError:
            raise BridgeError("managed_proxy_unavailable", "CLIProxyAPI could not start.") from None
    finally:
        os.close(descriptor)
        os.close(lease_fd)
    record = {"version": 1, "port": port, "pid": process.pid,
              "start": _pid_start(process.pid), "binary": str(binary),
              "lease_fd": lease_fd}
    try:
        _write_record(account_dir, record)
    except BaseException:
        _stop_record(record, account_dir)
        raise
    if not _ready(port, management_key, process):
        _stop_record(record, account_dir)
        raise BridgeError("managed_proxy_unavailable", "CLIProxyAPI did not become ready.")
    return {"record": record, "process": process,
            "client_key": client_key, "management_key": management_key}


class Supervisor:
    def __init__(self, directory):
        self.directory = _managed_root(Path(directory).parent)
        if self.directory != Path(directory).resolve():
            raise BridgeError("unsafe_store", "The managed proxy directory is invalid.")
        self.auth_fd, self.auth_token = create_auth()
        self.lock = threading.RLock()
        self.running = {}
        self.stopping = threading.Event()
        self.last_activity = time.monotonic()

    def route(self, action, account_id, base_url=None):
        with self.lock:
            self.last_activity = time.monotonic()
            if action == "shutdown":
                for identity, active in list(self.running.items()):
                    _stop_record(active["record"], _account_dir(self.directory, identity))
                    if active["process"] is not None:
                        active["process"].poll()
                self.running.clear()
                self.stopping.set()
                return {"stopped": True}
            account_dir = _account_dir(self.directory, account_id)
            record = _read_record(account_dir)
            if action == 'snapshot_stop':
                if not supervisor_hold(self.directory.parent, account_id):
                    raise BridgeError('credential_snapshot_pending',
                                      'Credential capture must first fence the account.')
                active = self.running.get(account_id)
                if active:
                    _stop_record(active['record'], account_dir)
                    if active['process'] is not None:
                        active['process'].poll()
                elif record:
                    _stop_record(record, account_dir)
                self.running.pop(account_id, None)
                return {'stopped': True, 'account_id': account_id}
            if action == "retire":
                require_supervisor(self.directory.parent, account_id)
                active = self.running.get(account_id)
                if active is None and record is None:
                    if not read_stop_marker(account_dir, account_id):
                        raise BridgeError('managed_proxy_stop_unverified',
                                          'The managed proxy route is unavailable for stop verification.')
                    return {"retired": True, "upstream_credential_removed": False}
                if active:
                    _stop_record(active["record"], account_dir)
                    if active["process"] is not None:
                        active["process"].poll()
                elif record:
                    _stop_record(record, account_dir)
                write_stop_marker(account_dir, account_id,
                                  active['record'] if active else record)
                self.running.pop(account_id, None)
                (account_dir / "route.json").unlink(missing_ok=True)
                sync_directory(account_dir)
                return {"retired": True, "upstream_credential_removed": False}
            if action not in {"provision", "ensure", "recovery_probe", "reconnect"}:
                raise BridgeError("invalid_request", "Unknown managed proxy operation.")
            if action == 'recovery_probe':
                held = supervisor_hold(self.directory.parent, account_id)
                if held is None or held[1] != 'restore':
                    raise BridgeError('credential_snapshot_pending',
                                      'Only held restored credentials can be probed by the host.')
            elif action == 'reconnect':
                held = supervisor_hold(self.directory.parent, account_id)
                if held is None or held[1] not in {'reconnect_preparing', 'reconnect'}:
                    raise BridgeError('credential_snapshot_pending',
                                      'Only host-authorized reconnection can provision a login route.')
            else:
                require_supervisor(self.directory.parent, account_id)
            if action == "ensure" and record is None:
                raise BridgeError("managed_proxy_not_found", "The saved managed proxy route is unavailable.")
            if record:
                port = record["port"]
            else:
                port = _port()
            expected_url = f"http://127.0.0.1:{port}/v1"
            if base_url is not None and base_url != expected_url:
                raise BridgeError("proxy_binding_changed", "The managed proxy endpoint changed.")
            active = self.running.get(account_id)
            if active and (active["process"].poll() is not None if active["process"] is not None
                           else not _same_process(active["record"], account_dir)):
                self.running.pop(account_id, None)
                active = None
            if not active:
                if record:
                    active = _recover(record, account_dir, account_id)
                    if active is None and _same_process(record, account_dir):
                        raise BridgeError("managed_proxy_recovery_required",
                                          "The running proxy could not be reattached safely. Stop it explicitly before retrying.")
                if active is None:
                    binary = _binary(self.directory.parent)
                    active = _launch(binary, account_dir, account_id, port)
                self.running[account_id] = active
            client_env, management_env = _names(account_id)
            return {"proxy_base_url": expected_url, "key_env": client_env,
                    "management_key_env": management_env,
                    "client_key": active["client_key"],
                    "management_key": active["management_key"]}


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 2
    supervisor = Supervisor(arguments[0])
    lock_path = supervisor.directory / "supervisor.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        path = supervisor.directory / "supervisor.sock"
        if path.exists():
            details = path.lstat()
            if not stat.S_ISSOCK(details.st_mode) or details.st_uid != os.getuid():
                return 1
            path.unlink()
        with _socket_address(supervisor.directory) as address:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(address)
                os.chmod(path, 0o600)
                listener.listen(32)
                listener.settimeout(0.5)
                try:
                    while not supervisor.stopping.is_set():
                        try:
                            connection, _ = listener.accept()
                        except socket.timeout:
                            with supervisor.lock:
                                idle = (not supervisor.running and
                                        time.monotonic() - supervisor.last_activity > 30)
                            if idle:
                                break
                            continue
                        threading.Thread(target=_serve_connection,
                                         args=(connection, supervisor), daemon=True).start()
                finally:
                    path.unlink(missing_ok=True)
    finally:
        os.close(descriptor)
        os.close(supervisor.auth_fd)


if __name__ == "__main__":
    raise SystemExit(main())
