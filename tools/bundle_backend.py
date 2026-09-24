"""Prepare pinned runtime resources before invoking the standard wheel builder."""

from contextlib import contextmanager
import fcntl

from setuptools.build_meta import *  # noqa: F403
from setuptools.build_meta import build_sdist as _build_sdist
from setuptools.build_meta import build_wheel as _build_wheel

from prepare_bundle import CACHE, prepare_bundle


@contextmanager
def _wheel_lock():
    # Preparation writes architecture-specific archives into the shared source
    # tree. Keep the lock until setuptools has copied them into the wheel.
    CACHE.mkdir(parents=True, exist_ok=True)
    with (CACHE / ".wheel.lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    with _wheel_lock():
        prepare_bundle()
        return _build_wheel(wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory, config_settings=None):
    with _wheel_lock():
        prepare_bundle()
        return _build_sdist(sdist_directory, config_settings)
