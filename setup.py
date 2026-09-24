"""Mark the bundled runtime wheel for its supported Linux architecture."""

from setuptools import setup
from setuptools.command.bdist_wheel import bdist_wheel
import os
import platform


class BundledWheel(bdist_wheel):
    def finalize_options(self):
        super().finalize_options()
        arch = os.environ.get("AGENTBRIDGE_TARGET_ARCH", platform.machine().lower())
        if arch == "arm64":
            arch = "aarch64"
        if platform.system() != "Linux" or arch not in {"x86_64", "aarch64"}:
            raise ValueError("Bundled AgentBridge wheels support Linux x86_64 and aarch64.")
        self.root_is_pure = False
        self.plat_name = "manylinux_2_28_" + arch

    def get_tag(self):
        return "py3", "none", self.plat_name


setup(cmdclass={"bdist_wheel": BundledWheel})
