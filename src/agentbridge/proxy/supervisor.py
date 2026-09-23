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
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

from ..errors import BridgeError
from ..models import identifier
from .managed import _managed_root, _names, _socket_address
from .route import ProxyRoute


MAX_REQUEST = 4096
MAX_RESPONSE = 16 * 1024
READY_SECONDS = 15


def _binary():
    configured = os.environ.get("AGENTBRIDGE_CLIPROXY_BIN")
    candidates = ([configured] if configured else []) + [
        str(Path(sys.executable).with_name("cliproxy")),
        shutil.which("cliproxy"), shutil.which("cli-proxy-api")]
    for candidate in candidates:
        if candidate:
            path = Path(candidate).expanduser().resolve()
            if path.is_file() and os.access(path, os.X_OK):
                return path
    raise BridgeError("proxy_binary_unavailable",
                      "Install CLIProxyAPI or set AGENTBRIDGE_CLIPROXY_BIN for this local AgentBridge environment.")


def _port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _pid_start(pid):
    try:
        value = Path(f"/proc/{pid}/stat").read_text()
        return value[value.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        return None


def _same_process(record, account_dir):
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 1 or _pid_start(pid) != record.get("start"):
        return False
    try:
        process = Path(f"/proc/{pid}")
        if process.stat().st_uid != os.getuid() or (process / "cwd").resolve() != account_dir:
            return False
        arguments = (process / "cmdline").read_bytes().split(b"\0")
        binary = Path(record["binary"]).resolve()
        return any(Path(os.fsdecode(arg)).resolve() == binary for arg in arguments[:2] if arg)
    except (OSError, KeyError, ValueError):
        return False


def _stop_record(record, account_dir):
    if not _same_process(record, account_dir):
        return
    pid = record["pid"]
    try:
        if os.getpgid(pid) != pid:
            return
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(30):
        if not _same_process(record, account_dir):
            return
        time.sleep(0.1)
    if _same_process(record, account_dir):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


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


def _proxy_config(port, auth_dir, client_key, management_key):
    return {"host": "127.0.0.1", "port": port, "auth-dir": str(auth_dir),
            "api-keys": [client_key], "request-retry": 0,
            "remote-management": {"allow-remote": False,
                                  "secret-key": management_key,
                                  "disable-control-panel": True,
                                  "disable-auto-update-panel": True},
            "plugins": {"enabled": False}, "discovery": {"enabled": False},
            "pprof": {"enable": False}, "logging-to-file": False}


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
                env=_proxy_environment())
        except OSError:
            raise BridgeError("managed_proxy_unavailable", "CLIProxyAPI could not start.") from None
    finally:
        os.close(descriptor)
        os.close(lease_fd)
    record = {"version": 1, "port": port, "pid": process.pid,
              "start": _pid_start(process.pid), "binary": str(binary),
              "lease_fd": lease_fd}
    _write_record(account_dir, record)
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
            if action == "retire":
                active = self.running.pop(account_id, None)
                if active:
                    _stop_record(active["record"], account_dir)
                    if active["process"] is not None:
                        active["process"].poll()
                elif record:
                    _stop_record(record, account_dir)
                (account_dir / "route.json").unlink(missing_ok=True)
                return {"retired": True, "upstream_credential_removed": False}
            if action not in {"provision", "ensure"}:
                raise BridgeError("invalid_request", "Unknown managed proxy operation.")
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
                    binary = _binary()
                    active = _launch(binary, account_dir, account_id, port)
                self.running[account_id] = active
            client_env, management_env = _names(account_id)
            return {"proxy_base_url": expected_url, "key_env": client_env,
                    "management_key_env": management_env,
                    "client_key": active["client_key"],
                    "management_key": active["management_key"]}


def _serve_connection(connection, supervisor):
    with connection:
        try:
            credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, peer_uid, _ = struct.unpack("3i", credentials)
            if peer_uid != os.getuid():
                return
            connection.settimeout(45)
            with connection.makefile("rb") as stream:
                line = stream.readline(MAX_REQUEST + 1)
            if not line or len(line) > MAX_REQUEST or not line.endswith(b"\n"):
                raise BridgeError("invalid_request", "The managed proxy request is invalid.")
            request = json.loads(line)
            if (not isinstance(request, dict) or
                    set(request) not in ({"action", "account_id"},
                                         {"action", "account_id", "base_url"})):
                raise BridgeError("invalid_request", "The managed proxy request is invalid.")
            result = supervisor.route(request["action"], request["account_id"],
                                      request.get("base_url"))
            response = {"ok": True, "result": result}
        except BridgeError as error:
            response = {"ok": False, "error": {"code": error.code, "message": str(error)}}
        except (OSError, TypeError, ValueError, KeyError):
            response = {"ok": False, "error": {"code": "managed_proxy_unavailable",
                                                "message": "The managed proxy request failed."}}
        payload = json.dumps(response, separators=(",", ":")).encode() + b"\n"
        if len(payload) <= MAX_RESPONSE:
            try:
                connection.sendall(payload)
            except OSError:
                pass


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


if __name__ == "__main__":
    raise SystemExit(main())
