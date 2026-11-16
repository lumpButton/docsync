import ast
import sys
import textwrap

import pytest

from docsync.model import (
    DESCRIPTION_PLACEHOLDER,
    VALUE_KEY,
    Definition,
    Entry,
    Function,
    Model,
    attribute_entries,
    build_model,
    is_api_object,
    raised_exceptions,
    skip_reason,
    squash,
    undeclared_attributes,
)

NESTED_YIELD: str = (
    "def f() -> Callable[[], Iterator[int]]:\n    def g():\n        yield 1\n    return g\n"
)


def _parse(code: str) -> tuple[str, list[Definition]]:
    source: str = textwrap.dedent(code)
    tree: ast.Module = ast.parse(source)
    nodes: list[Definition] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    ]
    return source, nodes


def _named(nodes: list[Definition], name: str) -> Definition:
    return next(node for node in nodes if node.name == name)


def _function(nodes: list[Definition], name: str) -> Function:
    node: Definition = _named(nodes, name)
    assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    return node


def _class(nodes: list[Definition], name: str) -> ast.ClassDef:
    node: Definition = _named(nodes, name)
    assert isinstance(node, ast.ClassDef)
    return node


def _section(model: Model, title: str) -> list[Entry]:
    return next((list(s.entries) for s in model.sections if s.title == title), [])


def test_args_cover_every_parameter_kind() -> None:
    source, nodes = _parse(
        """
        from fastapi import Path, Query
        class C:
            def m(self, a: int, /, b: str = "x", *args: int, c: bool, d: float = 1.5,
                  _: int = 0, **kwargs: str) -> None: ...
        def f(
            ws: Annotated[int, Path(description="Workspace id")],
            q: str = Query(default="x", description="Search text"),
        ) -> None: ...
        """
    )
    args: list[Entry] = _section(build_model(source, _named(nodes, "m"), method=True), "Args")
    assert [(e.key, e.head, e.tail) for e in args] == [
        ("a", "a (int)", None),
        ("b", "b (str)", 'Defaults to `"x"`.'),
        ("*args", "*args (int)", None),
        ("c", "c (bool)", None),
        ("d", "d (float)", "Defaults to `1.5`."),
        ("**kwargs", "**kwargs (str)", None),
    ]
    assert all(e.description == DESCRIPTION_PLACEHOLDER for e in args)

    api: list[Entry] = _section(build_model(source, _named(nodes, "f"), method=False), "Args")
    assert [(e.key, e.tail, e.description) for e in api] == [
        ("ws", None, "Workspace id."),
        ("q", None, "Search text."),  # a FastAPI Query(...) call is not a default value
    ]
    assert api[0].head == 'ws (Annotated[int, Path(description="Workspace id")])'


def test_cls_and_untyped_arguments() -> None:
    source, nodes = _parse(
        "class C:\n    @classmethod\n    def make(cls, raw, n=2) -> int: ...\n"
        "def find(cls: type, self: int) -> None: ...\n"
    )
    args: list[Entry] = _section(build_model(source, _named(nodes, "make"), method=True), "Args")
    assert [(e.key, e.head, e.tail) for e in args] == [
        ("raw", "raw", None),
        ("n", "n", "Defaults to `2`."),
    ]
    # outside a class `cls` / `self` are ordinary parameters and must be documented
    plain: list[Entry] = _section(build_model(source, _named(nodes, "find"), method=False), "Args")
    assert [e.key for e in plain] == ["cls", "self"]


def test_raises_follow_handlers_and_skip_nested_scopes() -> None:
    source, nodes = _parse(
        """
        def f(x: int) -> int:
            def inner() -> None:
                raise RuntimeError("nested scope")
            if x < 0:
                raise ValueError(x)
            if x == 0:
                raise errors.LeadError
            try:
                return int(x)
            except (KeyError, TypeError):
                raise
            except LookupError as err:
                raise err from None
            except OSError as other:
                raise make_error(other)
            raise ValueError("again")
        def unbound() -> None:
            raise err
        """
    )
    assert raised_exceptions(source, _function(nodes, "f")) == [
        "ValueError",
        "errors.LeadError",
        "KeyError",
        "TypeError",
        "LookupError",
    ]
    assert raised_exceptions(source, _function(nodes, "unbound")) == []


def test_raises_bare_class_names_and_innermost_handlers() -> None:
    source, nodes = _parse(
        """
        def f(x: int) -> None:
            if x:
                raise ValueError
            if x > 1:
                raise mod.Error
            try:
                pass
            except OSError as err:
                try:
                    pass
                except KeyError:
                    raise
                try:
                    pass
                except LookupError as err:
                    raise err
        """
    )
    assert raised_exceptions(source, _function(nodes, "f")) == [
        "ValueError",
        "mod.Error",
        "KeyError",  # bare raise: the innermost handler, not OSError
        "LookupError",  # `raise err`: the innermost handler binding `err`
    ]


@pytest.mark.skipif(sys.version_info < (3, 14), reason="PEP 758 syntax needs Python 3.14")
def test_pep758_except_without_parentheses() -> None:
    source, nodes = _parse(
        "def f() -> None:\n    try:\n        pass\n    except KeyError, TypeError:\n        raise\n"
    )
    assert raised_exceptions(source, _function(nodes, "f")) == ["KeyError", "TypeError"]


@pytest.mark.parametrize(
    ("code", "returns", "yields"),
    [
        ("def f() -> list[int]:\n    return []\n", "list[int]", None),
        ("def f() -> None:\n    return None\n", None, None),
        ("def f() -> NoReturn:\n    raise SystemExit\n", None, None),
        ("def f() -> Iterator[int]:\n    yield 1\n", None, "int"),
        ("async def f() -> AsyncIterator[dict[str, int]]:\n    yield {}\n", None, "dict[str, int]"),
        (
            "def f() -> Generator[tuple[int, int], None, str]:\n    yield 1, 2\n    return 'x'\n",
            "str",
            "tuple[int, int]",
        ),
        ("def f():\n    yield 1\n", None, ""),
        ("def f() -> Never:\n    raise SystemExit\n", None, None),
        ("def f() -> Iterator[int]:\n    yield 1\n    return\n", None, "int"),  # bare return
        ("def f() -> Generator[int, None, None]:\n    yield 1\n    return None\n", None, "int"),
        # a yield in a nested function does not make the outer one a generator
        (NESTED_YIELD, "Callable[[], Iterator[int]]", None),
        ("def f(x):\n    return x\n", "", None),  # no annotation yet: an untyped Returns
    ],
)
def test_returns_and_yields(code: str, returns: str | None, yields: str | None) -> None:
    source, nodes = _parse(code)
    model: Model = build_model(source, nodes[0], method=False)
    returns_entries: list[Entry] = _section(model, "Returns")
    yields_entries: list[Entry] = _section(model, "Yields")
    # "" stands for an entry without a type
    assert [e.head for e in returns_entries] == ([] if returns is None else [returns or None])
    if yields is None:
        assert yields_entries == []
    else:
        assert [e.head for e in yields_entries] == [yields or None]
        assert yields_entries[0].key == VALUE_KEY


def test_attributes_of_a_class() -> None:
    source, nodes = _parse(
        """
        class Lead(Base):
            MAX: ClassVar[int] = 3
            source: str
            phone: str | None = None
            tags: list[str] = field(default_factory=list)
            kind = "lead"
            normalize: Callable[[str], str] = staticmethod(lambda raw: raw)
            _id: int
            __slots_hint__ = "x"
            a = b = 1

            @property
            def id(self) -> int:
                return self._id

            @property
            def status(self) -> str:
                return self.kind

            @status.setter
            def status(self, value: str) -> None:
                self.kind = value

            @cached_property
            def masked(self) -> str:
                return ""

            @property
            def _internal(self) -> int:
                return 1
        """
    )
    entries: list[Entry] = attribute_entries(source, _class(nodes, "Lead"))
    assert [(e.key, e.head, e.tail) for e in entries] == [
        ("MAX", "MAX (ClassVar[int])", None),
        ("source", "source (str)", None),
        ("phone", "phone (str | None)", "Defaults to `None`."),
        ("tags", "tags (list[str])", None),
        ("kind", "kind", 'Defaults to `"lead"`.'),
        ("normalize", "normalize (Callable[[str], str])", None),
        ("id", "id (int)", "Read-only."),
        ("status", "status (str)", None),
    ]


def test_enum_and_pydantic_attributes() -> None:
    source, nodes = _parse(
        """
        class Status(StrEnum):
            NEW = "new"
            TAKEN = "taken"

        class LeadIn(BaseModel):
            model_config = ConfigDict(frozen=True)
            name: str = Field(min_length=1, description="Client name")
            phone: Annotated[str, Field(description="Phone in any format.")]
        """
    )
    status: list[Entry] = attribute_entries(source, _class(nodes, "Status"))
    assert [(e.key, e.head, e.tail) for e in status] == [
        ("NEW", "NEW", None),
        ("TAKEN", "TAKEN", None),
    ]
    lead_in: list[Entry] = attribute_entries(source, _class(nodes, "LeadIn"))
    assert [(e.key, e.description) for e in lead_in] == [
        ("model_config", "Pydantic model configuration."),
        ("name", "Client name."),
        ("phone", "Phone in any format."),
    ]


def test_class_args_come_from_init() -> None:
    source, nodes = _parse(
        "class A:\n    x: int\n    def __init__(self, x: int, *, y: str = 'a') -> None:\n"
        "        self.x = x\n        self.y = y\n        self._z = 1\n        self.x = 2\n"
    )
    cls: Definition = _named(nodes, "A")
    model: Model = build_model(source, cls, method=False)
    assert [e.key for e in _section(model, "Args")] == ["x", "y"]
    assert [e.key for e in _section(model, "Attributes")] == ["x"]
    assert isinstance(cls, ast.ClassDef)
    assert undeclared_attributes(source, cls) == ["y"]


def test_cached_property_has_no_sections() -> None:
    source, nodes = _parse(
        "class A:\n    @cached_property\n    def v(self) -> int:\n        return 1\n"
    )
    assert build_model(source, _named(nodes, "v"), method=True).sections == ()


@pytest.mark.parametrize(
    ("decorator", "reason"),
    [
        ("@overload", "D418"),
        ("@property", "Attributes section"),
        ("@value.setter", "setters"),
        ("@value.deleter", "setters"),
        ("@functools.cached_property", None),
        ("@override", None),
    ],
)
def test_skip_reason(decorator: str, reason: str | None) -> None:
    _, nodes = _parse(f"class A:\n    {decorator}\n    def value(self) -> int: ...\n")
    found: str | None = skip_reason(_named(nodes, "value"))
    assert found is None if reason is None else reason in (found or "")


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("@router.get('/x')\nasync def f() -> None: ...\n", True),
        ("@app.api_route('/x')\ndef f() -> None: ...\n", True),
        ("@router.dependency\ndef f() -> None: ...\n", False),
        ("@cache\ndef f() -> None: ...\n", False),
        ("class M(pydantic.BaseModel): ...\n", True),
        ("class S(BaseSettings): ...\n", True),
        ("class D(Base): ...\n", False),
    ],
)
def test_is_api_object(code: str, expected: bool) -> None:
    _, nodes = _parse(code)
    assert is_api_object(nodes[0]) is expected


def test_small_argument_and_attribute_rules() -> None:
    source, nodes = _parse(
        """
        def f(a: int, __: int, *_: int, **__kw: int) -> None: ...
        def g(x: str = foo(description="not FastAPI")) -> None: ...
        class Plain:
            model_config = {"frozen": True}
        """
    )
    args: list[Entry] = _section(build_model(source, _named(nodes, "f"), method=False), "Args")
    assert [e.key for e in args] == ["a", "**__kw"]  # underscore-only names are skipped
    other: list[Entry] = _section(build_model(source, _named(nodes, "g"), method=False), "Args")
    assert other[0].description == DESCRIPTION_PLACEHOLDER  # only Field/Path/Query/... count
    plain: list[Entry] = attribute_entries(source, _class(nodes, "Plain"))
    assert plain[0].description == DESCRIPTION_PLACEHOLDER  # not a Pydantic model


def test_undeclared_attributes_are_preserved_in_the_model() -> None:
    source, nodes = _parse(
        "class Box:\n    def __init__(self, size: int) -> None:\n        self.size = size\n"
        "        self.size = 2\n        def helper() -> None:\n            self.inner = 1\n"
    )
    cls: ast.ClassDef = _class(nodes, "Box")
    assert undeclared_attributes(source, cls) == ["size"]  # once; nested scopes ignored
    assert build_model(source, cls, method=False).preserved == frozenset({"size"})


def test_squash() -> None:
    assert squash("dict[\n    str,\n    list[int],\n]") == "dict[str, list[int]]"
    assert squash("Annotated[\n  int,\n  Path( ge=1 ),\n]") == "Annotated[int, Path(ge=1)]"
