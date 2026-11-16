import ast
import json
import sys
import textwrap
from pathlib import Path

import pytest

from docsync.edits import (
    Diagnostic,
    FixResult,
    action_at,
    docstrings_configured,
    find_outdated,
    find_placeholders,
    fix_source,
    is_test_file,
    line_length,
    missing_docstring_lines,
    project_root,
)
from docsync.model import DESCRIPTION_PLACEHOLDER, SUMMARY_PLACEHOLDER

FIXTURES: Path = Path(__file__).parent / "fixtures"
# A sample project with the ruff setup docsync is meant for; ruff reads it by file location.
SAMPLE: Path = Path(__file__).parent / "project"
APP: Path = SAMPLE / "app"
REPO: Path = Path(__file__).parents[1]


def _fix(code: str, missing: set[int] | None = None, length: int = 100) -> FixResult:
    source: str = textwrap.dedent(code)
    lines: set[int] = missing if missing is not None else _all_definition_lines(source)
    return fix_source(source, Path("app/m.py"), length, lines)


def _all_definition_lines(source: str) -> set[int]:
    return {
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


@pytest.mark.parametrize(
    "name",
    [
        # the functions fixture uses PEP 758 `except A, B:`, which only Python 3.14 parses
        pytest.param(
            "functions",
            marks=pytest.mark.skipif(sys.version_info < (3, 14), reason="needs Python 3.14"),
        ),
        "attributes",
        "api",
        "sync",
    ],
)
def test_fix_matches_reference_output(name: str) -> None:
    """References were checked against the earlier Lua implementation and all linters."""
    source: str = (FIXTURES / f"{name}.before.txt").read_text(encoding="utf-8")
    expected: str = (FIXTURES / f"{name}.expected.txt").read_text(encoding="utf-8")
    path: Path = APP / f"{name}.py"
    missing: set[int] = missing_docstring_lines(source, path, SAMPLE)
    result: FixResult = fix_source(source, path, 100, missing)
    assert result.source == expected
    again: FixResult = fix_source(
        result.source, path, 100, missing_docstring_lines(result.source, path, SAMPLE)
    )
    assert again.source == result.source
    assert (again.inserted, again.updated) == (0, 0)


@pytest.mark.parametrize(
    "module", sorted((REPO / "src" / "docsync").glob("*.py")), ids=lambda p: p.name
)
def test_fix_leaves_its_own_documented_code_alone(module: Path) -> None:
    """The tool's own docstrings are hand-written; fix must not lose or change any of them."""
    source: str = module.read_text(encoding="utf-8")
    result: FixResult = fix_source(
        source, module, 100, missing_docstring_lines(source, module, REPO)
    )
    assert result.source == source


def test_ruff_decides_where_docstrings_are_required() -> None:
    source: str = textwrap.dedent(
        """
        from typing import override

        class Base:
            def run(self) -> int:
                return 1

        class Child(Base):
            @override
            def run(self) -> int:
                return 2

            def _private(self) -> None:
                pass

        def outer() -> int:
            def inner() -> int:
                return 1
            return inner()
        """
    )
    missing: set[int] = missing_docstring_lines(source, APP / "m.py", SAMPLE)
    assert missing == {4, 5, 8, 16}  # Base, Base.run, Child, outer
    assert missing_docstring_lines(source, SAMPLE / "tests" / "test_m.py", SAMPLE) == set()


def test_inline_bodies_are_split() -> None:
    result: FixResult = _fix(
        """
        class Clock(Protocol):
            def now(self) -> datetime: ...  # keep me
            def tick(self) -> int: return 1  # and me
            def stub(self) -> None: pass
        """
    )
    assert result.source.split("\n")[1:3] == [
        "class Clock(Protocol):",
        f'    """{SUMMARY_PLACEHOLDER}"""',
    ]
    assert result.source.split("\n")[3] == ""  # blank line after a class docstring, as ruff format
    assert result.source.split("\n")[4:14] == [
        "    def now(self) -> datetime:",
        '        """TODO: summary.',
        "",
        "        Returns:",
        f"            datetime: {DESCRIPTION_PLACEHOLDER}",
        '        """',
        "        # keep me",
        "    def tick(self) -> int:",
        '        """TODO: summary.',
        "",
    ]
    assert "        return 1  # and me" in result.source
    assert "pass" not in result.source
    ast.parse(result.source)


def test_multiline_body_after_colon_is_reported_not_touched() -> None:
    code: str = "def f() -> int: return (\n    1\n)\n"
    result: FixResult = _fix(code)
    assert result.source == code
    assert [w.code for w in result.warnings] == ["DSW002"]


def test_template_goes_before_a_decorated_first_member() -> None:
    result: FixResult = _fix(
        """
        class Child(Base):
            @override
            def run(self) -> int:
                return 2
        """,
        missing={2},
    )
    assert result.source.split("\n")[1:6] == [
        "class Child(Base):",
        f'    """{SUMMARY_PLACEHOLDER}"""',
        "",
        "    @override",
        "    def run(self) -> int:",
    ]


def test_short_and_single_quoted_docstrings_are_left_alone() -> None:
    code: str = textwrap.dedent(
        """
        def short(a: int) -> int:
            \"\"\"Add.\"\"\"
            return a

        def quoted(a: int, b: int) -> int:
            '''Quoted.

            Args:
                a (int): A.
            '''
            return a
        """
    )
    result: FixResult = _fix(code, missing=set())
    assert result.source == code
    assert result.updated == 0


def test_undeclared_instance_attributes_are_warned() -> None:
    result: FixResult = _fix(
        """
        class Lead:
            source: str
            def __init__(self, source: str) -> None:
                self.source = source
                self.created_at = 1
        """
    )
    assert [(w.code, w.line) for w in result.warnings] == [("DSW001", 2)]
    assert "created_at" in result.warnings[0].message


def test_placeholders_and_outdated_docstrings() -> None:
    source: str = textwrap.dedent(
        '''
        """TODO: summary."""

        def send(chat_id: int, text: str) -> None:
            """Send a card.

            Args:
                chat_id (int): TODO: description.
            """
            # TODO: comments are not docstrings
            note: str = "TODO"

        def ok(x: int) -> None:
            """Parse TODOLIST handles.

            Args:
                x (int): X.
            """
        '''
    )
    path: Path = Path("app/m.py")
    placeholders: list[Diagnostic] = find_placeholders(source, path)
    assert [(d.line, d.column, d.code) for d in placeholders] == [
        (2, 4, "DSP001"),
        (8, 24, "DSP001"),
    ]
    assert placeholders[1].render() == (
        "app/m.py:8:24: DSP001 Docstring placeholder: chat_id (int): TODO: description."
    )
    outdated: list[Diagnostic] = find_outdated(source, path, 100)
    assert [(d.line, d.code) for d in outdated] == [(5, "DSP002")]
    assert "`send`" in outdated[0].message


def test_action_inserts_a_snippet() -> None:
    source: str = "def f(x: int) -> int:\n    return x\n"
    response: dict[str, object] = action_at(source, Path("app/m.py"), 2, 100)
    assert response["edits"] == [{"start": 1, "end": 1, "lines": ["    "]}]
    body: list[str] = [
        f'"""${{1:{SUMMARY_PLACEHOLDER}}}',
        "",
        "Args:",
        f"    x (int): ${{2:{DESCRIPTION_PLACEHOLDER}}}",
        "",
        "Returns:",
        f"    int: ${{3:{DESCRIPTION_PLACEHOLDER}}}",
        '"""',
    ]
    assert response["snippet"] == {"row": 1, "column": 4, "body": "\n".join(body)}

    stub: dict[str, object] = action_at("def g() -> None:\n    pass\n", Path("app/m.py"), 2, 100)
    assert stub["edits"] == [{"start": 1, "end": 2, "lines": ["    "]}]  # the lone `pass` goes
    json.dumps(response)


def test_action_updates_and_redirects() -> None:
    source: str = textwrap.dedent(
        '''
        class Lead:
            """Lead.

            Attributes:
                name (str): Name.
            """

            name: str

            @property
            def code(self) -> int:
                return 1

            @code.setter
            def code(self, value: int) -> None:
                pass
        '''
    )
    path: Path = Path("app/m.py")
    response: dict[str, object] = action_at(source, path, 12, 100)
    assert str(response["message"]).endswith("updated from the code")
    assert "property" in str(response["message"])
    edits: object = response["edits"]
    assert isinstance(edits, list)
    assert f"        code (int): {DESCRIPTION_PLACEHOLDER} Read-only." not in edits[0]["lines"]
    assert f"        code (int): {DESCRIPTION_PLACEHOLDER}" in edits[0]["lines"]  # has a setter

    setter: dict[str, object] = action_at(source, path, 16, 100)
    assert "setters" in str(setter["message"])
    assert setter["edits"] == []
    outside: dict[str, object] = action_at(source, path, 1, 100)
    assert "not inside" in str(outside["message"])


def _twice(code: str) -> tuple[str, str]:
    """Fix with the real ruff, then fix the result again."""
    path: Path = APP / "m.py"
    source: str = textwrap.dedent(code)
    first: str = fix_source(source, path, 100, missing_docstring_lines(source, path, SAMPLE)).source
    second: str = fix_source(first, path, 100, missing_docstring_lines(first, path, SAMPLE)).source
    return first, second


def test_dotted_exception_names_are_stable() -> None:
    first, second = _twice(
        """
        def find(x: int) -> int:
            if x < 0:
                raise ValueError(x)
            raise errors.NotFoundError(x)
        """
    )
    assert second == first
    assert first.count("errors.NotFoundError:") == 1
    described: str = first.replace(
        "errors.NotFoundError: TODO: description.", "errors.NotFoundError: Gone."
    )
    assert _twice(described)[0] == described


def test_human_text_survives_updates() -> None:
    code: str = '''
    class Box:
        """A box.

        Attributes:
            size (int): Size in centimetres, written by hand.
        """

        def __init__(self, size: int) -> None:
            self.size = size

    def total(x, y=1):
        """Add.

        Args:
            x: First.
            y: Second. Defaults to `1`.

        Returns:
            The sum of x and y.
        """
        return x + y
    '''
    first, second = _twice(code)
    assert second == first
    assert "size (int): Size in centimetres, written by hand." in first
    assert "Returns:\n        The sum of x and y." in first


def test_docstring_sharing_its_line_with_code_is_left_alone() -> None:
    code: str = (
        'def f(a: int) -> None:\n    """S.\n\n    Args:\n        b (int): B.\n    """; print(a)\n'
    )
    result: FixResult = _fix(code, missing=set())
    assert (result.source, result.updated) == (code, 0)


def test_non_ascii_inline_body_is_split_at_the_right_column() -> None:
    result: FixResult = _fix('def f(x: str = "ёж") -> str: return x  # keep\n')
    lines: list[str] = result.source.split("\n")
    assert lines[0] == 'def f(x: str = "ёж") -> str:'
    assert "    return x  # keep" in lines
    assert 'x (str): TODO: description. Defaults to `"ёж"`.' in result.source


def test_fix_refuses_to_return_broken_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("docsync.edits.render_docstring", lambda *_, **__: ['"""x'])
    with pytest.raises(RuntimeError, match="would break"):
        _fix("def f() -> None:\n    return\n")


def test_ruff_failure_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class Failed:
        returncode: int = 2
        stdout: str = ""
        stderr: str = "ruff failed: Failed to parse pyproject.toml"

    monkeypatch.setattr("docsync.edits.subprocess.run", lambda *_, **__: Failed())
    with pytest.raises(RuntimeError, match="Failed to parse"):
        missing_docstring_lines("def f() -> None: ...\n", APP / "m.py", SAMPLE)


def test_ruff_sees_the_real_path_from_any_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(SAMPLE.parent)  # paths below are relative to this, not to the project root
    source: str = "def test_x() -> None:\n    pass\n"
    assert missing_docstring_lines(source, Path("project/tests/test_x.py"), SAMPLE) == set()
    assert missing_docstring_lines(source, Path("project/app/x.py"), SAMPLE) == {1}


UNDOCUMENTED: str = textwrap.dedent(
    """
    class Lead:
        def __init__(self) -> None:
            pass

        def __repr__(self) -> str:
            return ""

        def take(self) -> None:
            pass

        def _secret(self) -> None:
            pass

    def find() -> None:
        pass

    def _helper() -> None:
        pass
    """
)


def _project(tmp_path: Path, ruff: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(f"[project]\nname = 'x'\n{ruff}", encoding="utf-8")
    return tmp_path


def test_works_without_any_ruff_docstring_setup(tmp_path: Path) -> None:
    root: Path = _project(tmp_path, "")
    app: Path = root / "app" / "lead.py"
    assert not docstrings_configured(app, root)
    # public class, method and function; not __init__, magic or private ones
    assert missing_docstring_lines(UNDOCUMENTED, app, root) == {2, 9, 15}
    for test_file in ("tests/test_lead.py", "pkg/tests/helpers.py", "test_x.py", "x_test.py"):
        assert missing_docstring_lines(UNDOCUMENTED, root / test_file, root) == set(), test_file
    assert missing_docstring_lines(UNDOCUMENTED, root / "conftest.py", root) == set()


def test_a_configured_project_is_followed_as_is(tmp_path: Path) -> None:
    root: Path = _project(
        tmp_path,
        '[tool.ruff.lint]\nselect = ["D"]\nignore = ["D102"]\n'
        '[tool.ruff.lint.per-file-ignores]\n"migrations/*" = ["D"]\n',
    )
    app: Path = root / "app" / "lead.py"
    assert docstrings_configured(app, root)
    assert missing_docstring_lines(UNDOCUMENTED, app, root) == {2, 15}  # D102 is ignored
    # a per-file ignore is respected, also on the fallback path
    assert missing_docstring_lines(UNDOCUMENTED, root / "migrations" / "m.py", root) == set()
    # tests are not excluded by name when the project selects D for them
    assert missing_docstring_lines(UNDOCUMENTED, root / "tests" / "test_x.py", root) == {2, 15}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("tests/test_a.py", True),
        ("tests/helpers.py", True),
        ("src/pkg/test/data.py", True),
        ("test_a.py", True),
        ("a_test.py", True),
        ("conftest.py", True),
        ("app/attest.py", False),
        ("app/contest.py", False),
        ("app/testing.py", False),
        ("latest/a.py", False),
    ],
)
def test_is_test_file(tmp_path: Path, name: str, expected: bool) -> None:
    assert is_test_file(tmp_path / name, tmp_path) is expected
    # directories above the project root do not count
    assert (
        is_test_file(tmp_path / "tests" / "proj" / "app" / "a.py", tmp_path / "tests" / "proj")
        is False
    )


def test_nested_public_class_is_required() -> None:
    source: str = "class Outer:\n    class Inner:\n        pass\n"
    assert missing_docstring_lines(source, APP / "m.py", SAMPLE) == {1, 2}


def test_action_on_unparsable_code() -> None:
    response: dict[str, object] = action_at("def f(:\n", Path("app/m.py"), 1, 100)
    assert "does not parse" in str(response["message"])
    assert response["edits"] == []


def test_action_in_init_documents_the_class() -> None:
    source: str = "class A:\n    def __init__(self, x: int) -> None:\n        self.x = x\n"
    response: dict[str, object] = action_at(source, Path("app/m.py"), 3, 100)
    assert response["message"] is None  # no "property" prefix for __init__
    # the snippet line, then the blank line ruff format wants after a class docstring
    assert response["edits"] == [{"start": 1, "end": 1, "lines": ["    ", ""]}]
    snippet: object = response["snippet"]
    assert isinstance(snippet, dict)
    assert "Args:\n    x (int): ${2:TODO: description.}" in str(snippet["body"])
    warnings: object = response["warnings"]
    assert isinstance(warnings, list)
    assert len(warnings) == 1
    assert "x:" in str(warnings[0])


def test_action_on_a_decorator_line_and_an_inline_body() -> None:
    source: str = "class A:\n    @staticmethod\n    def m() -> int: return 1\n"
    response: dict[str, object] = action_at(source, Path("app/m.py"), 2, 100)
    assert response["edits"] == [
        {"start": 2, "end": 3, "lines": ["    def m() -> int:", "        ", "        return 1"]}
    ]
    snippet: object = response["snippet"]
    assert isinstance(snippet, dict)
    assert (snippet["row"], snippet["column"]) == (3, 8)


def test_project_settings(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 120\n", encoding="utf-8")
    nested: Path = tmp_path / "pkg" / "mod.py"
    nested.parent.mkdir()
    nested.write_text("", encoding="utf-8")
    assert project_root(nested) == tmp_path.resolve()
    assert line_length(tmp_path) == 120
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    assert line_length(tmp_path) == 88
    assert line_length(tmp_path / "missing") == 88
    assert line_length(SAMPLE) == 100
