"""Local OAuth browser launching never reuses a signed-in profile."""

import os
from pathlib import Path
import signal
import stat
import subprocess
from threading import Event
import time

import pytest

from playground import auth_browser


URLS = {
    'codex': 'https://auth.openai.com/oauth/authorize?state=fixture',
    'claude': 'https://claude.ai/oauth/authorize?state=fixture',
    'grok': 'https://auth.x.ai/oauth2/device/verify?user_code=fixture',
}


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid
        self.finished = Event()
        self.returncode = None
        self.signals = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.finished.wait(timeout):
            raise subprocess.TimeoutExpired('fixture-chrome', timeout)
        self.returncode = 0
        return 0

    def send_signal(self, action):
        self.signals.append(action)
        self.finished.set()


class FakePopen:
    def __init__(self):
        self.calls = []
        self.processes = {}

    def __call__(self, argv, **options):
        process = FakeProcess(800000 + len(self.calls))
        self.calls.append((argv, options))
        self.processes[process.pid] = process
        return process


def _attempt(provider='claude', *, attempt_id='fixture-attempt', url=None,
             status='awaiting_user'):
    return {'attempt_id': attempt_id, 'provider': provider, 'status': status,
            'authorization_url': URLS[provider] if url is None else url}


@pytest.fixture
def local_profiles(tmp_path, monkeypatch):
    # Force the non-tmpfs branch so each test can inspect its own private root.
    monkeypatch.setattr(auth_browser, 'SHARED_MEMORY_ROOT', tmp_path / 'unavailable')
    return tmp_path / 'profiles'


def _profile(popen):
    argument = next(value for value in popen.calls[-1][0]
                    if value.startswith('--user-data-dir='))
    return Path(argument.split('=', 1)[1])


def _wait_until_removed(path):
    deadline = time.monotonic() + 2
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not path.exists()


def test_launch_uses_private_profile_and_sanitized_environment(
        local_profiles, monkeypatch):
    popen = FakePopen()
    monkeypatch.setenv('DISPLAY', ':7')
    monkeypatch.setenv('AGENTBRIDGE_MANAGEMENT_KEY', 'fixture-secret')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture-other-secret')
    monkeypatch.setattr(auth_browser.os, 'killpg',
                        lambda pid, action: popen.processes[pid].send_signal(action))
    browser = auth_browser.IsolatedAuthBrowser(local_profiles,
                                                browser='/fixture/chromium', popen=popen)
    assert browser.launch(_attempt()) is True
    argv, options = popen.calls[0]
    profile = _profile(popen)
    assert argv == ['/fixture/chromium', f'--user-data-dir={profile}', '--incognito',
                    '--new-window', '--no-first-run', '--no-default-browser-check',
                    URLS['claude']]
    assert profile.parent == local_profiles
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700
    assert [item.name for item in profile.iterdir()] == [auth_browser.OWNED_MARKER]
    assert URLS['claude'].encode() not in (profile / auth_browser.OWNED_MARKER).read_bytes()
    assert options['env']['DISPLAY'] == ':7'
    assert 'AGENTBRIDGE_MANAGEMENT_KEY' not in options['env']
    assert 'ANTHROPIC_API_KEY' not in options['env']
    assert 'fixture-secret' not in repr(options)
    assert options['start_new_session'] is True
    assert options['close_fds'] is True
    assert options['stdin'] == options['stdout'] == options['stderr'] == subprocess.DEVNULL
    assert browser.active('fixture-attempt') is True
    assert browser.launch(_attempt()) is True
    assert len(popen.calls) == 1
    browser.stop('fixture-attempt')
    _wait_until_removed(profile)
    assert popen.processes[800000].signals == [signal.SIGTERM]
    assert browser.active('fixture-attempt') is False
    assert browser.launch(_attempt()) is False


@pytest.mark.parametrize('attempt', [
    _attempt(status='authorized'),
    _attempt(url='http://claude.ai/oauth/authorize'),
    _attempt(url='https://claude.ai.evil.test/oauth/authorize'),
    _attempt(url='https://other@claude.ai/oauth/authorize'),
    _attempt(url='https://claude.ai:8443/oauth/authorize'),
    _attempt(url='https://claude.ai/oauth/authorize#token'),
    _attempt(url='https://claude.ai/oauth/authorize\nother'),
    _attempt(url='https://evil.test\\@claude.ai/oauth/authorize'),
    _attempt(url='https://auth.openai.com/oauth/authorize'),
    {**_attempt(), 'provider': ['claude']},
])
def test_unsafe_or_mismatched_attempt_does_not_spawn(local_profiles, attempt):
    popen = FakePopen()
    browser = auth_browser.IsolatedAuthBrowser(local_profiles,
                                                browser='/fixture/chromium', popen=popen)
    assert browser.launch(attempt) is False
    assert popen.calls == []
    assert not local_profiles.exists()


def test_absent_browser_and_failed_spawn_keep_no_profile(local_profiles, monkeypatch):
    monkeypatch.setattr(auth_browser.shutil, 'which', lambda name: None)
    browser = auth_browser.IsolatedAuthBrowser(local_profiles)
    assert browser.launch(_attempt()) is False
    assert not local_profiles.exists()

    def fail_spawn(*args, **kwargs):
        raise OSError('fixture-browser-unavailable')

    browser = auth_browser.IsolatedAuthBrowser(local_profiles,
                                                browser='/fixture/chromium', popen=fail_spawn)
    assert browser.launch(_attempt()) is False
    assert list(local_profiles.iterdir()) == []


def test_profile_removed_when_browser_exits_and_close_stops_every_session(
        local_profiles, monkeypatch):
    popen = FakePopen()
    monkeypatch.setattr(auth_browser.os, 'killpg',
                        lambda pid, action: popen.processes[pid].send_signal(action))
    browser = auth_browser.IsolatedAuthBrowser(local_profiles,
                                                browser='/fixture/chromium', popen=popen)
    assert browser.launch(_attempt('claude', attempt_id='first'))
    first = _profile(popen)
    assert browser.launch(_attempt('codex', attempt_id='second'))
    second = _profile(popen)
    assert first != second
    popen.processes[800000].finished.set()
    _wait_until_removed(first)
    assert not browser.active('first')
    assert browser.active('second')
    browser.close()
    _wait_until_removed(second)
    assert not browser.active('second')
    assert browser.launch(_attempt('grok', attempt_id='third')) is False


def test_prefers_roomy_shared_memory_without_writing_state_root(tmp_path, monkeypatch):
    shared = tmp_path / 'shared'
    shared.mkdir()
    monkeypatch.setattr(auth_browser, 'SHARED_MEMORY_ROOT', shared)
    monkeypatch.setattr(auth_browser, 'MIN_SHARED_MEMORY_BYTES', 1)
    popen = FakePopen()
    monkeypatch.setattr(auth_browser.os, 'killpg',
                        lambda pid, action: popen.processes[pid].send_signal(action))
    state_root = tmp_path / 'state'
    browser = auth_browser.IsolatedAuthBrowser(state_root,
                                                browser='/fixture/chromium', popen=popen)
    assert browser.launch(_attempt('grok'))
    profile = _profile(popen)
    assert profile.parent == shared
    assert not state_root.exists()
    browser.close()
    _wait_until_removed(profile)


def test_stale_cleanup_preserves_unrelated_and_active_profiles(local_profiles, monkeypatch):
    local_profiles.mkdir()
    stale = local_profiles / 'agentbridge-oauth-stale'
    stale.mkdir(mode=0o700)
    marker = stale / auth_browser.OWNED_MARKER
    marker.write_bytes(auth_browser.MARKER_CONTENT)
    old = time.time() - auth_browser.STALE_PROFILE_AGE_SECONDS - 5
    os.utime(marker, (old, old))
    unrelated = local_profiles / 'agentbridge-oauth-unrelated'
    unrelated.mkdir()
    in_use = local_profiles / 'agentbridge-oauth-active'
    in_use.mkdir(mode=0o700)
    in_use_marker = in_use / auth_browser.OWNED_MARKER
    in_use_marker.write_bytes(auth_browser.MARKER_CONTENT)
    os.utime(in_use_marker, (old, old))
    monkeypatch.setattr(auth_browser, '_profile_in_use', lambda profile: profile == in_use)
    popen = FakePopen()
    monkeypatch.setattr(auth_browser.os, 'killpg',
                        lambda pid, action: popen.processes[pid].send_signal(action))
    browser = auth_browser.IsolatedAuthBrowser(local_profiles,
                                                browser='/fixture/chromium', popen=popen)
    assert browser.launch(_attempt())
    assert not stale.exists()
    assert unrelated.exists()
    assert in_use.exists()
    browser.close()
