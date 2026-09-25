"""Open same-host OAuth in a short-lived, isolated Chromium profile."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit


AUTH_HOSTS = {
    'codex': frozenset({'auth.openai.com', 'auth0.openai.com'}),
    'claude': frozenset({'claude.ai', 'claude.com', 'platform.claude.com',
                         'console.anthropic.com'}),
    'grok': frozenset({'auth.x.ai', 'x.ai', 'accounts.x.ai', 'grok.com'}),
}
BROWSER_COMMANDS = ('google-chrome', 'google-chrome-stable', 'chromium',
                    'chromium-browser')
SHARED_MEMORY_ROOT = Path('/dev/shm')
MIN_SHARED_MEMORY_BYTES = 128 * 1024 * 1024
PROFILE_PREFIX = 'agentbridge-oauth-'
OWNED_MARKER = '.agentbridge-browser-profile'
MARKER_CONTENT = b'agentbridge-isolated-browser-v1\n'
STALE_PROFILE_AGE_SECONDS = 60
DESKTOP_ENVIRONMENT = frozenset({
    'PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR',
    'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'XDG_RUNTIME_DIR',
    'XDG_SESSION_TYPE', 'XDG_CURRENT_DESKTOP', 'DBUS_SESSION_BUS_ADDRESS',
    'GDK_BACKEND', 'FONTCONFIG_PATH', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
})


def _authorization(attempt):
    """Accept only a supported provider's HTTPS authorization host."""
    if not isinstance(attempt, dict) or attempt.get('status') != 'awaiting_user':
        return None
    attempt_id = attempt.get('attempt_id')
    provider = attempt.get('provider')
    value = attempt.get('authorization_url')
    if (not isinstance(attempt_id, str) or not 1 <= len(attempt_id) <= 256
            or not isinstance(value, str) or not 1 <= len(value) <= 8192
            or not isinstance(provider, str) or provider not in AUTH_HOSTS
            or any(character == '\\' or character.isspace()
                   or ord(character) < 32 or ord(character) == 127
                   for character in value)):
        return None
    try:
        url = urlsplit(value)
        valid = (url.scheme == 'https' and url.hostname in AUTH_HOSTS[provider]
                 and url.port in (None, 443) and not url.username
                 and not url.password and not url.fragment)
    except ValueError:
        valid = False
    return (attempt_id, value) if valid else None


def _browser_command(configured):
    if configured is not None:
        command = str(configured)
        return command or None
    for command in BROWSER_COMMANDS:
        available = shutil.which(command)
        if available:
            return available
    return None


def _profile_parent(root):
    """Prefer a memory filesystem when it has room for a Chromium profile."""
    if root == SHARED_MEMORY_ROOT or SHARED_MEMORY_ROOT in root.parents:
        return root
    try:
        available = os.statvfs(SHARED_MEMORY_ROOT)
        free_bytes = available.f_bavail * available.f_frsize
        if (SHARED_MEMORY_ROOT.is_dir() and os.access(SHARED_MEMORY_ROOT, os.W_OK)
                and free_bytes >= MIN_SHARED_MEMORY_BYTES):
            return SHARED_MEMORY_ROOT
    except OSError:
        pass
    return root


def _profile_in_use(profile):
    """Leave a profile in place unless procfs proves our browser is gone."""
    argument = f'--user-data-dir={profile}'.encode()
    try:
        processes = tuple(Path('/proc').iterdir())
    except OSError:
        return True
    for process in processes:
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            command = (process / 'cmdline').read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if argument in command.split(b'\x00'):
            return True
    return False


def _remove_stale_profiles(parent, active_profiles):
    """Remove only older profiles with our marker and no running browser."""
    try:
        candidates = tuple(parent.glob(f'{PROFILE_PREFIX}*'))
    except OSError:
        return
    for profile in candidates:
        if profile in active_profiles or profile.is_symlink() or not profile.is_dir():
            continue
        marker = profile / OWNED_MARKER
        try:
            if (marker.read_bytes() != MARKER_CONTENT
                    or time.time() - marker.stat().st_mtime < STALE_PROFILE_AGE_SECONDS):
                continue
        except OSError:
            continue
        if not _profile_in_use(profile):
            shutil.rmtree(profile, ignore_errors=True)


class BrowserSession:
    def __init__(self, process, profile):
        self.process = process
        self.profile = profile


class IsolatedAuthBrowser:
    """Own browser subprocesses without changing the SDK's OAuth state."""

    def __init__(self, root, *, browser=None, popen=None):
        self.root = Path(root).expanduser().resolve()
        self.browser = browser
        self._popen = popen or subprocess.Popen
        self._sessions = {}
        self._launched = set()
        self._lock = threading.Lock()
        self._closed = False

    def launch(self, attempt):
        """Open one fresh profile per pending attempt; never relaunch a replay."""
        authorization = _authorization(attempt)
        if authorization is None:
            return False
        attempt_id, url = authorization
        with self._lock:
            if self._closed:
                return False
            if attempt_id in self._launched:
                session = self._sessions.get(attempt_id)
                return session is not None and session.process.poll() is None
            command = _browser_command(self.browser)
            if command is None:
                return False
            parent = _profile_parent(self.root)
            profile = None
            try:
                parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _remove_stale_profiles(parent, {item.profile for item in self._sessions.values()})
                profile = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX, dir=parent))
                profile.chmod(0o700)
                marker_fd = os.open(profile / OWNED_MARKER,
                                    os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(marker_fd, 'wb') as marker:
                    marker.write(MARKER_CONTENT)
            except OSError:
                if profile is not None:
                    shutil.rmtree(profile, ignore_errors=True)
                return False
            argv = [command, f'--user-data-dir={profile}', '--incognito',
                    '--new-window', '--no-first-run', '--no-default-browser-check', url]
            environment = {key: value for key, value in os.environ.items()
                           if key in DESKTOP_ENVIRONMENT}
            try:
                process = self._popen(
                    argv, env=environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, close_fds=True,
                )
            except (OSError, ValueError):
                shutil.rmtree(profile, ignore_errors=True)
                return False
            if process.poll() is not None:
                shutil.rmtree(profile, ignore_errors=True)
                return False
            session = BrowserSession(process, profile)
            self._sessions[attempt_id] = session
            self._launched.add(attempt_id)
        watcher = threading.Thread(target=self._watch, args=(attempt_id, session), daemon=True)
        try:
            watcher.start()
        except RuntimeError:
            self.stop(attempt_id)
            return False
        return True

    def active(self, attempt_id):
        """Whether the isolated browser process is still running."""
        with self._lock:
            session = self._sessions.get(attempt_id)
            return session is not None and session.process.poll() is None

    def _watch(self, attempt_id, session):
        try:
            session.process.wait()
        except OSError:
            return
        self._finish(attempt_id, session)

    def _finish(self, attempt_id, session):
        with self._lock:
            if self._sessions.get(attempt_id) is not session:
                return
            del self._sessions[attempt_id]
        shutil.rmtree(session.profile, ignore_errors=True)

    @staticmethod
    def _signal(process, action):
        try:
            os.killpg(process.pid, action)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.send_signal(action)
            except OSError:
                pass

    def stop(self, attempt_id):
        """End our browser process group and remove its profile after exit."""
        with self._lock:
            session = self._sessions.get(attempt_id)
        if session is None:
            return
        process = session.process
        if process.poll() is None:
            self._signal(process, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._signal(process, signal.SIGKILL)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    return
        self._finish(attempt_id, session)

    def close(self):
        with self._lock:
            self._closed = True
            attempts = tuple(self._sessions)
        for attempt_id in attempts:
            self.stop(attempt_id)
