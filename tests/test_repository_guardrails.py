"""Regression cases for the guard, including Git partial staging."""
from pathlib import Path
import subprocess

import pytest

from tools.check_repository import check_file, check_repository


def test_repository_respects_guardrails():
    assert check_repository(Path(__file__).resolve().parents[1]) == []


@pytest.mark.parametrize("suffix", [".py", ".md", ".json", ".yml"])
def test_line_limit_counts_comments_and_blank_lines(suffix):
    path = Path("example" + suffix)
    assert check_file(path, b"\n" * 450) == []
    assert any("451 lines" in finding for finding in check_file(path, b"\n" * 451))


def test_final_line_without_newline_counts():
    assert any("451 lines" in finding for finding in check_file(Path("sample.md"), b"\n" * 450 + b"last"))


@pytest.mark.parametrize("path, source, expected", [
    ("src/badName.py", "", "module names"),
    ("docs/bad_name.md", "", "Markdown filenames"),
    ("tests/account.py", "", "test modules"),
    ("src/utils.py", "", "catch-all"),
    ("src/account.py", "def badName(accountId): pass", "snake_case"),
    ("src/account.py", "class account_service: pass", "PascalCase"),
    ("src/account.py", "accountId = 1", "variable"),
])
def test_naming_violations(path, source, expected):
    assert any(expected in finding for finding in check_file(Path(path), source.encode()))


def test_supported_conventions_and_provider_wire_keys():
    source = 'MAX_LINES = 450\nclass AccountService:\n    def __init__(self, account_id):\n        self.data = {"planType": account_id}\n'
    assert check_file(Path("src/accounts.py"), source.encode()) == []
    assert check_file(Path("docs/README.md"), b"# Index\n") == []


def test_symlink_cannot_hide_large_source():
    assert any("symlinks" in finding for finding in check_file(Path("source.py"), None))


def test_staged_bytes_are_checked_even_if_worktree_was_fixed(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / "example.py"
    path.write_text("\n" * 451)
    subprocess.run(["git", "-C", str(tmp_path), "add", "example.py"], check=True)
    path.write_text("# Fixed but not staged\n")
    assert check_repository(tmp_path) == []
    assert any("451 lines" in finding for finding in check_repository(tmp_path, staged=True))


def test_untracked_source_is_checked_but_ignored_build_output_is_not(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("build/\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build/generated.py").write_text("\n" * 451)
    assert check_repository(tmp_path) == []
    (tmp_path / "new.py").write_text("\n" * 451)
    assert any("new.py: 451 lines" in finding for finding in check_repository(tmp_path))
