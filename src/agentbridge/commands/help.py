"""Terminal-aware help formatting."""
import argparse
import os
import re
import sys


_RESET = "\033[0m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"


def _color_enabled():
    """Use color only for an interactive terminal unless explicitly forced off."""
    return bool(getattr(sys.stdout, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ


class PrettyHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keep argparse layout, then add restrained terminal colors."""

    def format_help(self):
        text = super().format_help()
        if not _color_enabled():
            return text
        text = re.sub(r"(?m)^usage:", f"{_BOLD}{_CYAN}usage:{_RESET}", text)
        text = re.sub(
            r"(?m)^([A-Za-z][^\n]*):$",
            lambda match: f"{_BOLD}{_YELLOW}{match.group(1)}:{_RESET}",
            text,
        )
        text = re.sub(
            r"(?m)^(\s+)(-[^\s,]+(?:,\s*--[^\s]+)?)(?=\s+)",
            lambda match: f"{match.group(1)}{_GREEN}{match.group(2)}{_RESET}",
            text,
        )
        return text


def add_parser(subparsers, name, **kwargs):
    kwargs.setdefault("formatter_class", PrettyHelpFormatter)
    return subparsers.add_parser(name, **kwargs)
