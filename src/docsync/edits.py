"""Line edits that add docstring templates and keep existing docstrings in sync with the code.

Where a docstring is required is decided by ruff itself (D101, D102, D103, D106), so the rules
and exceptions (private names, ``@override``, ``@property``, per-file ignores) are never
duplicated here. A project whose ruff config does not ask for docstrings gets those four rules
switched on for docsync's query, tests excluded.
"""

import ast
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from docsync.google import Mode, parse_docstring, render_docstring
from docsync.model import (
    FUNCTIONS,
    Definition,
    Model,
    accessor_kind,
    build_model,
    skip_reason,
    undeclared_attributes,
)

MISSING_DOCSTRING_CODES: frozenset[str] = frozenset({"D101", "D102", "D103", "D106"})
# Undocumented public class, method and function: ruff flags them when docstrings are required.
PROBE: str = (
    "class Probe:\n    def probe(self) -> None:\n        pass\n\n\ndef probe() -> None:\n    pass\n"
)
PLACEHOLDER: re.Pattern[str] = re.compile(r"\bTODO\b")
DEFAULT_LINE_LENGTH: int = 88
INDENT_STEP: str = "    "


@dataclass(frozen=True, slots=True)
class Edit:
    """Replace lines ``[start, end)`` (0-based) with ``lines``.

    Attributes:
        start (int): First replaced line.
        end (int): Line after the last replaced one; equal to ``start`` for an insertion.
        lines (tuple[str, ...]): New lines.
    """

    start: int
    end: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Snippet:
    """An LSP snippet to expand after the edits are applied.

    Attributes:
        row (int): 0-based line of the snippet.
        column (int): Column of the snippet start.
        body (str): Snippet body; lines after the first are relative to ``column``.
    """

    row: int
    column: int
    body: str


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A finding in ruff's ``path:line:col: CODE message`` format.

    Attributes:
        path (Path): File.
        line (int): 1-based line.
        column (int): 1-based column.
        code (str): ``DSP001`` placeholder, ``DSP002`` outdated docstring, ``DSW001`` warning.
        message (str): Explanation.
    """

    path: Path
    line: int
    column: int
    code: str
    message: str

    def render(self) -> str:
        """Format like ruff.

        Returns:
            str: ``path:line:col: CODE message``.
        """
        return f"{self.path}:{self.line}:{self.column}: {self.code} {self.message}"


@dataclass(slots=True)
class FixResult:
    """Outcome of :func:`fix_source`.

    Attributes:
        source (str): Fixed source.
        inserted (int): Templates added. Defaults to `0`.
        updated (int): Existing docstrings brought in sync. Defaults to `0`.
        warnings (list[Diagnostic]): Things the tool cannot fix itself.
    """

    source: str
    inserted: int = 0
    updated: int = 0
    warnings: list[Diagnostic] = field(default_factory=list)


def project_root(path: Path) -> Path:
    """Directory of the nearest ``pyproject.toml`` above ``path``.

    Args:
        path (Path): File or directory.

    Returns:
        Path: Project root, or the current directory when there is none.
    """
    start: Path = path.resolve()
    for directory in (start, *start.parents):
        if (directory / "pyproject.toml").is_file():
            return directory
    return Path.cwd()


def line_length(root: Path) -> int:
    """Maximum line length of the project: ruff ``line-length``.

    Args:
        root (Path): Project root.

    Returns:
        int: The configured line length, 88 by default as in ruff.
    """
    pyproject: Path = root / "pyproject.toml"
    if not pyproject.is_file():
        return DEFAULT_LINE_LENGTH
    config: dict[str, object] = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    tool: object = config.get("tool", {})
    ruff: object = tool.get("ruff", {}) if isinstance(tool, dict) else {}
    value: object = ruff.get("line-length", DEFAULT_LINE_LENGTH) if isinstance(ruff, dict) else None
    return value if isinstance(value, int) else DEFAULT_LINE_LENGTH


def is_test_file(path: Path, root: Path) -> bool:
    """Tell whether a file is a test: under ``tests``/``test``, ``test_*.py``, ``*_test.py``.

    Args:
        path (Path): The file.
        root (Path): Project root; directories above it are not looked at.

    Returns:
        bool: True for test modules and ``conftest.py``.
    """
    resolved: Path = path.resolve()
    relative: Path = (
        resolved.relative_to(root) if resolved.is_relative_to(root) else Path(path.name)
    )
    name: str = relative.name
    in_tests: bool = any(part in {"tests", "test"} for part in relative.parts[:-1])
    return (
        in_tests or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
    )


def _missing_docstrings(source: str, path: Path, root: Path, extra: list[str]) -> set[int]:
    """Run ruff on ``source`` as if it were ``path``; lines with D101/D102/D103/D106 findings."""
    command: list[str] = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--output-format",
        "json",
        "--no-fix",
        "--quiet",
        *extra,
        "--stdin-filename",
        str(path.resolve()),  # ruff runs in the project root: per-file ignores need the real path
        "-",
    ]
    result: subprocess.CompletedProcess[str] = subprocess.run(  # noqa: S603 - fixed argv, no shell
        command, input=source, capture_output=True, text=True, cwd=root, check=False
    )
    # exit 0: no findings, 1: findings; anything else is a ruff failure (bad config, crash)
    if result.returncode not in {0, 1}:
        raise RuntimeError(f"ruff failed: {result.stderr.strip() or result.stdout.strip()}")
    try:
        diagnostics: list[dict[str, object]] = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as err:
        raise RuntimeError(f"ruff failed: {result.stderr.strip()}") from err
    lines: set[int] = set()
    for diagnostic in diagnostics:
        location: object = diagnostic.get("location")
        if diagnostic.get("code") in MISSING_DOCSTRING_CODES and isinstance(location, dict):
            row: object = location.get("row")
            if isinstance(row, int):
                lines.add(row)
    return lines


def docstrings_configured(path: Path, root: Path) -> bool:
    """Tell whether the project's ruff config requires docstrings for this file.

    A probe with an undocumented public class, method and function is checked as if it were
    ``path``, so ``select``, ``ignore`` and ``per-file-ignores`` all count.

    Args:
        path (Path): The file.
        root (Path): Project root.

    Returns:
        bool: True when ruff reports any of D101, D102, D103 for the probe.
    """
    return bool(_missing_docstrings(PROBE, path, root, []))


def missing_docstring_lines(source: str, path: Path, root: Path) -> set[int]:
    """Lines of definitions that need a docstring.

    When the project's ruff config requires docstrings, ruff decides with that config as is.
    Otherwise docsync asks ruff for D101, D102, D103 and D106 itself (public classes, methods,
    functions and nested classes) and leaves test files alone, so it works without any setup.

    Args:
        source (str): Module source.
        path (Path): File name for ruff's configuration and per-file ignores.
        root (Path): Project root to run ruff from.

    Returns:
        set[int]: 1-based lines of ``def`` / ``class`` keywords.
    """
    if docstrings_configured(path, root):
        return _missing_docstrings(source, path, root, [])
    if is_test_file(path, root):
        return set()
    return _missing_docstrings(
        source, path, root, ["--extend-select", ",".join(sorted(MISSING_DOCSTRING_CODES))]
    )


def definitions(tree: ast.Module) -> Iterator[Definition]:
    """Every function and class of a module, nested ones included.

    Args:
        tree (ast.Module): Parsed module.

    Yields:
        Definition: Functions and classes.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield node


def docstring_literal(node: Definition) -> ast.Constant | None:
    """The docstring literal of a definition.

    Args:
        node (Definition): Function or class.

    Returns:
        ast.Constant | None: The string literal, or None when there is no docstring.
    """
    first: ast.stmt = node.body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return first.value
    return None


def _char_col(line: str, byte_col: int) -> int:
    """Convert an ast UTF-8 byte offset into a character index."""
    return len(line.encode()[:byte_col].decode(errors="ignore"))


def _indent_of(line: str) -> str:
    """Leading whitespace of a line."""
    return line[: len(line) - len(line.lstrip())]


def _indented(rendered: list[str], indent: str) -> list[str]:
    """Add the docstring indentation to rendered lines, keeping blank lines empty."""
    return [f"{indent}{line}" if line else "" for line in rendered]


def method_nodes(tree: ast.Module) -> set[ast.AST]:
    """Functions defined directly in a class body: their first ``self``/``cls`` is not documented.

    Args:
        tree (ast.Module): Parsed module.

    Returns:
        set[ast.AST]: The method nodes.
    """
    return {
        child
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for child in node.body
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def sync_edit(
    source: str, lines: list[str], node: Definition, model: Model, length: int
) -> tuple[str, Edit | None]:
    """Bring an existing docstring in line with the code.

    Short docstrings (no sections, which pydoclint accepts) and docstrings that are not
    ``\"\"\"...\"\"\"`` or share their lines with other code are left alone.

    Args:
        source (str): Module source.
        lines (list[str]): ``source`` split into lines.
        node (Definition): Function or class with a docstring.
        model (Model): What the docstring must contain.
        length (int): Maximum line length.

    Returns:
        tuple[str, Edit | None]: ``updated`` with the edit, ``unchanged`` or ``skipped``.
    """
    literal: ast.Constant | None = docstring_literal(node)
    text: str | None = ast.get_source_segment(source, literal) if literal else None
    parsed = parse_docstring(text) if text is not None else None
    if literal is None or literal.end_lineno is None or parsed is None or not parsed.sections:
        return "skipped", None
    start: int = literal.lineno - 1
    end: int = literal.end_lineno
    indent: str = lines[start][: _char_col(lines[start], literal.col_offset)]
    tail: str = lines[end - 1][_char_col(lines[end - 1], literal.end_col_offset or 0) :]
    if indent.strip() or tail.strip():
        return "skipped", None
    rendered: list[str] = render_docstring(model, indent, length, "plain", parsed)
    new_lines: list[str] = _indented(rendered, indent)
    if new_lines == lines[start:end]:
        return "unchanged", None
    return "updated", Edit(start, end, tuple(new_lines))


def _is_stub(statement: ast.stmt) -> bool:
    """``...`` or ``pass``: a placeholder body that a docstring replaces."""
    return isinstance(statement, ast.Pass) or (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and statement.value.value is Ellipsis
    )


def _split_inline_body(lines: list[str], node: Definition) -> tuple[int, str, list[str]] | None:
    """For ``def f(): ...`` return (row, indent, lines replacing that row) without the docstring."""
    first: ast.stmt = node.body[0]
    last: ast.stmt = node.body[-1]
    row: int = first.lineno - 1
    if last.end_lineno != first.lineno or last.end_col_offset is None:
        return None
    line: str = lines[row]
    head: str = line[: _char_col(line, first.col_offset)].rstrip()
    after: int = _char_col(line, last.end_col_offset)
    statements: str = line[_char_col(line, first.col_offset) : after].strip()
    comment: str = line[after:].strip()
    indent: str = _indent_of(lines[node.lineno - 1]) + INDENT_STEP
    rest: list[str] = []
    if len(node.body) == 1 and _is_stub(first):
        rest = [f"{indent}{comment}"] if comment else []
    else:
        rest = [f"{indent}{statements}" + (f"  {comment}" if comment else "")]
    return row, indent, [head, *rest]


def insert_edit(
    lines: list[str], node: Definition, model: Model, length: int, mode: Mode
) -> tuple[Edit, Snippet | None] | str:
    """Add a docstring template to a definition without one.

    A body written on the ``def`` line is moved to its own line; a ``...``/``pass`` stub is dropped.

    Args:
        lines (list[str]): Module source split into lines.
        node (Definition): Function or class without a docstring.
        model (Model): What the docstring must contain.
        length (int): Maximum line length.
        mode (Mode): ``plain`` inserts text; ``snippet`` inserts an empty line and a snippet.

    Returns:
        tuple[Edit, Snippet | None] | str: The edit and snippet, or why nothing can be inserted.
    """
    first: ast.stmt = node.body[0]
    decorators: list[ast.expr] = (
        first.decorator_list
        if isinstance(first, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        else []
    )
    # ast puts a decorated definition at its `def`/`class` line: the statement starts at `@`
    row: int = min([first.lineno, *(decorator.lineno for decorator in decorators)]) - 1
    inline: bool = not decorators and bool(
        lines[row][: _char_col(lines[row], first.col_offset)].strip()
    )
    if inline:
        split: tuple[int, str, list[str]] | None = _split_inline_body(lines, node)
        if split is None:
            return "a multi-line body after `:` must be moved to its own lines by hand"
        row, indent, replacement = split
        head, rest, end = [replacement[0]], replacement[1:], row + 1
        insert_at: int = row + 1
    else:
        indent = _indent_of(lines[row])
        head, rest, end, insert_at = [], [], row, row
        if len(node.body) == 1 and _is_stub(first) and lines[row].strip() in {"pass", "..."}:
            end = row + 1  # a lone `pass` / `...` becomes redundant next to a docstring (PIE790)
    rendered: list[str] = render_docstring(model, indent, length, mode)
    if mode == "snippet":
        body: list[str] = [indent]
        snippet: Snippet | None = Snippet(insert_at, len(indent), "\n".join(rendered))
    else:
        body, snippet = _indented(rendered, indent), None
    following: list[str] = rest or lines[end : end + 1]
    if isinstance(node, ast.ClassDef) and following and following[0].strip():
        body = [*body, ""]  # ruff format puts a blank line after a class docstring
    return Edit(row, end, (*head, *body, *rest)), snippet


def apply_edits(lines: list[str], edits: list[Edit]) -> list[str]:
    """Apply non-overlapping edits bottom-up so that earlier line numbers stay valid.

    Args:
        lines (list[str]): Original lines.
        edits (list[Edit]): Edits computed against ``lines``.

    Returns:
        list[str]: New lines.
    """
    result: list[str] = list(lines)
    for edit in sorted(edits, key=lambda item: (item.start, item.end), reverse=True):
        result[edit.start : edit.end] = edit.lines
    return result


def _warn_undeclared(source: str, path: Path, node: ast.ClassDef) -> Diagnostic | None:
    """Warning for public instance attributes that cannot be documented yet."""
    names: list[str] = undeclared_attributes(source, node)
    if not names:
        return None
    message: str = (
        f"{', '.join(names)}: assigned in __init__ but not declared in the class body; "
        "declare them as `name: type` to document them (pydoclint only checks those)"
    )
    return Diagnostic(path, node.lineno, node.col_offset + 1, "DSW001", message)


def fix_source(source: str, path: Path, length: int, missing: set[int]) -> FixResult:
    """Add templates where ruff requires a docstring and update existing docstrings.

    Args:
        source (str): Module source.
        path (Path): File name for messages.
        length (int): Maximum line length.
        missing (set[int]): Lines of definitions without a required docstring (from ruff).

    Returns:
        FixResult: New source, counters and warnings.

    Raises:
        RuntimeError: The edits would produce code that does not parse; nothing is returned.
    """
    tree: ast.Module = ast.parse(source)
    lines: list[str] = source.split("\n")
    result: FixResult = FixResult(source)
    edits: list[Edit] = []
    methods: set[ast.AST] = method_nodes(tree)
    for node in definitions(tree):
        if skip_reason(node) is not None:
            continue
        model: Model = build_model(source, node, method=node in methods)
        documented: bool = docstring_literal(node) is not None
        if documented:
            _, edit = sync_edit(source, lines, node, model, length)
            if edit is not None:
                edits.append(edit)
                result.updated += 1
        elif node.lineno in missing:
            inserted: tuple[Edit, Snippet | None] | str = insert_edit(
                lines, node, model, length, "plain"
            )
            if isinstance(inserted, str):
                result.warnings.append(Diagnostic(path, node.lineno, 1, "DSW002", inserted))
                continue
            edits.append(inserted[0])
            result.inserted += 1
            documented = True
        if documented and isinstance(node, ast.ClassDef):
            warning: Diagnostic | None = _warn_undeclared(source, path, node)
            if warning is not None:
                result.warnings.append(warning)
    result.source = "\n".join(apply_edits(lines, edits))
    try:
        ast.parse(result.source)
    except SyntaxError as err:
        # never hand back broken code: the caller keeps the original source
        raise RuntimeError(f"{path}: docstring edits would break the code: {err}") from err
    return result


def find_placeholders(source: str, path: Path) -> list[Diagnostic]:
    """Placeholders left in docstrings (DSP001).

    Args:
        source (str): Module source.
        path (Path): File name for messages.

    Returns:
        list[Diagnostic]: One per docstring line with a placeholder.
    """
    tree: ast.Module = ast.parse(source)
    lines: list[str] = source.split("\n")
    nodes: list[ast.Module | Definition] = [tree, *definitions(tree)]
    found: list[Diagnostic] = []
    for node in nodes:
        first: ast.stmt | None = node.body[0] if node.body else None
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and first.end_lineno is not None
        ):
            continue
        for number in range(first.lineno, first.end_lineno + 1):
            match: re.Match[str] | None = PLACEHOLDER.search(lines[number - 1])
            if match is not None:
                text: str = lines[number - 1].strip()
                found.append(
                    Diagnostic(
                        path, number, match.start() + 1, "DSP001", f"Docstring placeholder: {text}"
                    )
                )
    return sorted(found, key=lambda item: item.line)


def find_outdated(source: str, path: Path, length: int) -> list[Diagnostic]:
    """Docstrings that ``fix`` would update (DSP002).

    Args:
        source (str): Module source.
        path (Path): File name for messages.
        length (int): Maximum line length.

    Returns:
        list[Diagnostic]: One per outdated docstring, at the docstring.
    """
    tree: ast.Module = ast.parse(source)
    lines: list[str] = source.split("\n")
    found: list[Diagnostic] = []
    methods: set[ast.AST] = method_nodes(tree)
    for node in definitions(tree):
        literal: ast.Constant | None = docstring_literal(node)
        if literal is None or skip_reason(node) is not None:
            continue
        model: Model = build_model(source, node, method=node in methods)
        status, _ = sync_edit(source, lines, node, model, length)
        if status == "updated":
            message: str = f"Docstring of `{node.name}` is out of date with the code; run fix"
            found.append(
                Diagnostic(path, literal.lineno, literal.col_offset + 1, "DSP002", message)
            )
    return sorted(found, key=lambda item: item.line)


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """Map every node to its parent."""
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def target_at(tree: ast.Module, line: int) -> tuple[Definition, Definition | None] | None:
    """The innermost definition around a line; ``__init__`` and properties lead to their class.

    Args:
        tree (ast.Module): Parsed module.
        line (int): 1-based cursor line.

    Returns:
        tuple[Definition, Definition | None] | None: (target, original function when redirected to
            its class), or None outside any definition.
    """
    around: list[Definition] = [
        node
        for node in definitions(tree)
        if min([node.lineno, *(d.lineno for d in node.decorator_list)])
        <= line
        <= (node.end_lineno or node.lineno)
    ]
    if not around:
        return None
    node: Definition = max(around, key=lambda item: item.lineno)
    if isinstance(node, FUNCTIONS) and (
        node.name == "__init__" or accessor_kind(node) == "property"
    ):
        parent: ast.AST | None = _parents(tree).get(node)
        if isinstance(parent, ast.ClassDef):
            return parent, node
    return node, None


def _class_warning(source: str, path: Path, node: Definition) -> list[str]:
    """Undeclared-attribute warning of a class, as text."""
    if not isinstance(node, ast.ClassDef):
        return []
    warning: Diagnostic | None = _warn_undeclared(source, path, node)
    return [warning.message] if warning else []


def action_at(source: str, path: Path, line: int, length: int) -> dict[str, object]:
    """What ``<space>cs`` does at a cursor line: insert a snippet or update the docstring.

    Args:
        source (str): Module source.
        path (Path): File name for messages.
        line (int): 1-based cursor line.
        length (int): Maximum line length.

    Returns:
        dict[str, object]: ``message``, ``edits`` (``start``/``end``/``lines``), ``snippet``
            (``row``/``column``/``body`` or None) and ``warnings``.
    """
    response: dict[str, object] = {"message": None, "edits": [], "snippet": None, "warnings": []}
    try:
        tree: ast.Module = ast.parse(source)
    except SyntaxError as err:
        # unfinished code is the normal state while editing
        response["message"] = f"the code does not parse yet (line {err.lineno}): {err.msg}"
        return response
    lines: list[str] = source.split("\n")
    found: tuple[Definition, Definition | None] | None = target_at(tree, line)
    if found is None:
        response["message"] = "the cursor is not inside a function or class"
        return response
    node, origin = found
    reason: str | None = skip_reason(node)
    if reason is not None:
        response["message"] = reason
        return response
    response["warnings"] = _class_warning(source, path, node)
    model: Model = build_model(source, node, method=node in method_nodes(tree))
    edit: Edit | None
    if docstring_literal(node) is not None:
        status, edit = sync_edit(source, lines, node, model, length)
        messages: dict[str, str] = {
            "updated": "updated from the code",
            "unchanged": "already up to date",
            "skipped": "short (no sections) or not in triple double quotes: left as is",
        }
        prefix: str = (
            "the property is in class Attributes; " if origin and origin.name != "__init__" else ""
        )
        response["message"] = prefix + messages[status]
    else:
        inserted: tuple[Edit, Snippet | None] | str = insert_edit(
            lines, node, model, length, "snippet"
        )
        if isinstance(inserted, str):
            response["message"] = inserted
            return response
        edit, snippet = inserted
        if snippet is not None:
            response["snippet"] = {
                "row": snippet.row,
                "column": snippet.column,
                "body": snippet.body,
            }
    if edit is not None:
        response["edits"] = [{"start": edit.start, "end": edit.end, "lines": list(edit.lines)}]
    return response
