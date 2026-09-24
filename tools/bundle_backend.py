"""Prepare pinned runtime resources before invoking the standard wheel builder."""

from setuptools.build_meta import *  # noqa: F403
from setuptools.build_meta import build_sdist as _build_sdist
from setuptools.build_meta import build_wheel as _build_wheel

from prepare_bundle import prepare_bundle


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    prepare_bundle()
    return _build_wheel(wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory, config_settings=None):
    prepare_bundle()
    return _build_sdist(sdist_directory, config_settings)
