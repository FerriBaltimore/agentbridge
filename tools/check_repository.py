"""Check repository text size and naming, including staged Git contents."""
import argparse
import ast
from pathlib import Path
import re
import subprocess
import sys

MAX_LINES = 450
SNAKE_CASE = re.compile(r"_?[a-z][a-z0-9]*(?:_[a-z0-9]+)*|__[a-z][a-z0-9_]*__|_")
CONSTANT_CASE = re.compile(r"_?[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")
CLASS_CASE = re.compile(r"[A-Z][a-zA-Z0-9]*")
KEBAB_CASE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SPECIAL_NAMES = {"AGENTS.md", "README.md", "CLAUDE.md", "LICENSE", "NOTICE", "Makefile", "MANIFEST.in"}
OPAQUE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".pdf"}


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args])


def repository_files(root, *, staged=False):
    """Yield versioned bytes; untracked nonignored files also count locally."""
    if staged:
        for entry in git(root, "ls-files", "--stage", "-z").split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode, object_id, stage = metadata.decode().split()
            path = Path(raw_path.decode())
            if stage != "0" or mode not in {"100644", "100755"}:
                yield path, None
            else:
                yield path, git(root, "cat-file", "blob", object_id)
        return
    paths = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    for raw_path in sorted(set(paths.split(b"\0")) - {b""}):
        path = Path(raw_path.decode())
        absolute = root / path
        if absolute.is_symlink():
            yield path, None
        elif absolute.is_file():
            yield path, absolute.read_bytes()


def path_findings(path):
    findings = []
    for part in path.parts[:-1]:
        name = part.removeprefix(".")
        if not (SNAKE_CASE.fullmatch(name) or KEBAB_CASE.fullmatch(name)):
            findings.append(f"directory {part!r} must use lowercase snake_case or kebab-case")
    name = path.name
    if name in SPECIAL_NAMES or name.startswith("."):
        return findings
    if path.suffix == ".py":
        if not SNAKE_CASE.fullmatch(path.stem):
            findings.append("Python module names must use snake_case")
        if "tests" in path.parts and path.stem != "conftest" and not (
            path.stem.startswith("test_") or path.stem == "__init__"
        ):
            findings.append("test modules must start with test_")
    elif path.suffix == ".md":
        if not KEBAB_CASE.fullmatch(path.stem):
            findings.append("Markdown filenames must use kebab-case (except contract filenames)")
    elif not re.fullmatch(r"[a-z0-9_][a-z0-9_.-]*", name):
        findings.append("filenames must use lowercase names")
    if path.suffix == ".py" and path.stem in {"utils", "helpers", "common", "misc", "manager"}:
        findings.append("use a responsibility-specific module name, not a catch-all")
    return findings


def python_findings(source):
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return [f"line {error.lineno}: invalid Python syntax"]
    findings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            if not CLASS_CASE.fullmatch(node.name):
                findings.append(f"line {node.lineno}: class {node.name!r} must use PascalCase")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.arg)):
            name = node.arg if isinstance(node, ast.arg) else node.name
            if not SNAKE_CASE.fullmatch(name):
                findings.append(f"line {node.lineno}: {name!r} must use snake_case")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if not (SNAKE_CASE.fullmatch(node.id) or CONSTANT_CASE.fullmatch(node.id)):
                findings.append(f"line {node.lineno}: variable {node.id!r} must use snake_case or UPPER_SNAKE_CASE")
    return findings


def check_file(path, data):
    findings = path_findings(path)
    if data is None:
        return findings + ["symlinks, submodules and unresolved index entries cannot bypass repository checks"]
    if path.suffix in OPAQUE_SUFFIXES:
        return findings
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError:
        return findings + ["repository text must be UTF-8"]
    line_count = len(source.splitlines())
    if line_count > MAX_LINES:
        findings.append(f"{line_count} lines exceeds the {MAX_LINES}-line limit; split by responsibility")
    if path.suffix == ".py":
        findings.extend(python_findings(source))
    return findings


def check_repository(root, *, staged=False):
    return [f"{path}: {finding}" for path, data in repository_files(root, staged=staged)
            for finding in check_file(path, data)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Check exact staged blobs, including partial staging")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        findings = check_repository(args.root, staged=args.staged)
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as error:
        print(f"Repository check could not complete: {type(error).__name__}", file=sys.stderr)
        return 1
    for finding in findings:
        print(finding, file=sys.stderr)
    if not findings:
        print("Repository guardrails passed (450-line limit and naming conventions).")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
