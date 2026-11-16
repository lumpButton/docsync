"""Command line: ``fix``, ``check`` and ``at`` (for editors).

Examples::

    docsync fix                                 # the current directory
    docsync check app x.py                      # files and directories
    docsync fix --stdin-filename app/x.py -     # source on stdin, fixed source on stdout
    docsync at --line 42 --stdin-filename app/x.py -   # JSON for an editor action

Directories are walked recursively for ``*.py``; hidden directories (``.venv``, ``.git``,
caches) and ``__pycache__``, ``node_modules``, ``build``, ``dist`` are skipped.
"""

import argparse
import json
import sys
from collections.abc import Iterator, Sequence
from importlib.metadata import version
from pathlib import Path

from docsync.edits import (
    Diagnostic,
    FixResult,
    action_at,
    find_outdated,
    find_placeholders,
    fix_source,
    line_length,
    missing_docstring_lines,
    project_root,
)

SKIPPED_DIRS: frozenset[str] = frozenset({"__pycache__", "node_modules", "build", "dist"})


def _skipped(part: str) -> bool:
    """Directories that never hold project sources: hidden ones, caches, build output."""
    return part.startswith(".") or part in SKIPPED_DIRS


def _python_files(targets: Sequence[str]) -> Iterator[Path]:
    """Expand files and directories into Python files, in a stable order.

    Only the part of a path below the given directory is checked for skipped directories, so
    ``..`` or a hidden directory named explicitly still works.

    Args:
        targets (Sequence[str]): Files and directories from the command line.

    Yields:
        Path: Python files to process.
    """
    for target in targets:
        path: Path = Path(target)
        if path.is_dir():
            yield from sorted(
                file
                for file in path.rglob("*.py")
                if not any(_skipped(part) for part in file.relative_to(path).parts[:-1])
            )
        else:
            yield path


def _write(stream: object, diagnostics: list[Diagnostic]) -> None:
    """Print diagnostics, one per line."""
    if hasattr(stream, "write"):
        for diagnostic in diagnostics:
            stream.write(diagnostic.render() + "\n")


def _fix(path: Path, source: str) -> FixResult:
    """Fix one file's source, asking ruff where docstrings are required."""
    root: Path = project_root(path)
    return fix_source(source, path, line_length(root), missing_docstring_lines(source, path, root))


def command_fix(paths: Sequence[str], stdin_filename: str | None) -> int:
    """Add templates and update docstrings in files, or on stdin.

    Args:
        paths (Sequence[str]): Files or directories; ``-`` reads stdin.
        stdin_filename (str | None): File name of the stdin source.

    Returns:
        int: Exit code: 0 on success.
    """
    if list(paths) == ["-"]:
        result: FixResult = _fix(Path(stdin_filename or "stdin.py"), sys.stdin.read())
        sys.stdout.write(result.source)
        _write(sys.stderr, result.warnings)
        return 0
    changed: int = 0
    for path in _python_files(paths):
        source: str = path.read_text(encoding="utf-8")
        result = _fix(path, source)
        _write(sys.stderr, result.warnings)
        if result.source != source:
            path.write_text(result.source, encoding="utf-8")
            changed += 1
            sys.stdout.write(f"{path}: +{result.inserted} templates, {result.updated} updated\n")
    sys.stdout.write(f"{changed} file(s) changed\n")
    return 0


def command_check(paths: Sequence[str]) -> int:
    """Report docstring placeholders (DSP001) and outdated docstrings (DSP002).

    Args:
        paths (Sequence[str]): Files or directories.

    Returns:
        int: Exit code: 1 when anything is found.
    """
    findings: list[Diagnostic] = []
    for path in _python_files(paths):
        source: str = path.read_text(encoding="utf-8")
        length: int = line_length(project_root(path))
        findings.extend(find_placeholders(source, path))
        findings.extend(find_outdated(source, path, length))
    _write(sys.stdout, sorted(findings, key=lambda item: (str(item.path), item.line)))
    return 1 if findings else 0


def command_at(line: int, stdin_filename: str) -> int:
    """Print the ``<space>cs`` action at a cursor line as JSON; the source comes on stdin.

    Args:
        line (int): 1-based cursor line.
        stdin_filename (str): File name of the stdin source.

    Returns:
        int: Exit code: 0.
    """
    path: Path = Path(stdin_filename)
    response: dict[str, object] = action_at(
        sys.stdin.read(), path, line, line_length(project_root(path))
    )
    sys.stdout.write(json.dumps(response, ensure_ascii=False))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and run a command.

    Args:
        argv (Sequence[str] | None): Arguments without the program name; None reads sys.argv.
            Defaults to `None`.

    Returns:
        int: Process exit code.
    """
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="docsync", description=__doc__.split("\n")[0] if __doc__ else None
    )
    commands = parser.add_subparsers(dest="command", required=True)
    fix: argparse.ArgumentParser = commands.add_parser(
        "fix", help="add templates, update docstrings"
    )
    fix.add_argument("paths", nargs="*", default=["."])
    fix.add_argument("--stdin-filename")
    check: argparse.ArgumentParser = commands.add_parser("check", help="fail on placeholders")
    check.add_argument("paths", nargs="*", default=["."])
    commands.add_parser("version", help="print the version")
    at: argparse.ArgumentParser = commands.add_parser("at", help="editor action at a line (JSON)")
    at.add_argument("--line", type=int, required=True)
    at.add_argument("--stdin-filename", required=True)
    at.add_argument("source", choices=["-"])
    args: argparse.Namespace = parser.parse_args(argv)
    try:
        if args.command == "fix":
            return command_fix(args.paths, args.stdin_filename)
        if args.command == "check":
            return command_check(args.paths)
        if args.command == "version":
            sys.stdout.write(f"docsync {version('docsync')}\n")
            return 0
        return command_at(args.line, args.stdin_filename)
    except RuntimeError as err:
        # expected failures (ruff errors, refused edits) get one line, not a traceback
        sys.stderr.write(f"docsync: {err}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
