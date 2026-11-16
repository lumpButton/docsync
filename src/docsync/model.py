"""What the Google docstring of a function or class must contain, derived from its code.

The model lists the managed sections (Args, Attributes, Returns, Yields, Raises) with one entry
per documented item. Types come from annotations, descriptions from FastAPI/Pydantic
``description=`` where available, otherwise from placeholders that the check refuses to accept.
"""

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass

SUMMARY_PLACEHOLDER: str = "TODO: summary."
DESCRIPTION_PLACEHOLDER: str = "TODO: description."
VALUE_KEY: str = "__value"  # the single entry of Returns / Yields

type Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
type Function = ast.FunctionDef | ast.AsyncFunctionDef

SCOPES: tuple[type[ast.AST], ...] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
)
FUNCTIONS: tuple[type[Function], ...] = (ast.FunctionDef, ast.AsyncFunctionDef)
API_DESCRIBED_CALLS: frozenset[str] = frozenset(
    {"Field", "Path", "Query", "Header", "Cookie", "Body", "Form", "File"}
)
HTTP_ROUTE_METHODS: frozenset[str] = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace", "api_route", "websocket"}
)
PYDANTIC_BASES: frozenset[str] = frozenset({"BaseModel", "BaseSettings"})
YIELDING_GENERICS: frozenset[str] = frozenset(
    {"Iterator", "Iterable", "Generator", "AsyncIterator", "AsyncIterable", "AsyncGenerator"}
)
NO_RETURN: frozenset[str] = frozenset({"None", "NoReturn", "Never"})
ACCESSORS: frozenset[str] = frozenset({"property", "cached_property", "setter", "deleter"})
SIMPLE_DEFAULTS: tuple[type[ast.expr], ...] = (
    ast.Constant,
    ast.Name,
    ast.Attribute,
    ast.UnaryOp,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
)
GENERATOR_ARITY: int = 3  # Generator[Yield, Send, Return]


@dataclass(frozen=True, slots=True)
class Entry:
    """One item of a managed section.

    Attributes:
        key (str): Name used to match the entry with an existing docstring.
        head (str | None): ``name (type)``, ``name``, a bare type, or None for an untyped value.
        tail (str | None): Generated suffix such as ``Defaults to `3`.`` or ``Read-only.``.
        description (str): Description for a new entry.
    """

    key: str
    head: str | None
    tail: str | None
    description: str


@dataclass(frozen=True, slots=True)
class Section:
    """A managed docstring section.

    Attributes:
        title (str): ``Args``, ``Attributes``, ``Returns``, ``Yields`` or ``Raises``.
        entries (tuple[Entry, ...]): Entries in code order.
    """

    title: str
    entries: tuple[Entry, ...]


@dataclass(frozen=True, slots=True)
class Model:
    """What the docstring of a definition must contain.

    Attributes:
        sections (tuple[Section, ...]): Non-empty managed sections in canonical order.
        api (bool): FastAPI endpoint or Pydantic model: sections go after ``\\f``.
        preserved (frozenset[str]): Attribute names that are never generated but whose existing
            entries must be kept (public ``self.x`` of ``__init__``).
    """

    sections: tuple[Section, ...]
    api: bool
    preserved: frozenset[str] = frozenset()


def squash(text: str) -> str:
    """Collapse a possibly multi-line annotation into one line.

    Args:
        text (str): Source text of an expression.

    Returns:
        str: The same expression on one line, without trailing commas inside brackets.
    """
    flat: str = re.sub(r"\s+", " ", text)
    flat = re.sub(r",\s*([\])])", r"\1", flat)
    flat = re.sub(r"([\[(])\s+", r"\1", flat)
    flat = re.sub(r"\s+([\])])", r"\1", flat)
    return flat.strip()


def _segment(source: str, node: ast.AST) -> str:
    """Source text of a node, squashed to one line."""
    text: str | None = ast.get_source_segment(source, node)
    return squash(text if text is not None else ast.unparse(node))


def last_name(node: ast.expr) -> str | None:
    """Return the last dotted segment of a name, attribute or call.

    Args:
        node (ast.expr): ``name``, ``mod.name`` or ``mod.name(...)``.

    Returns:
        str | None: ``name``, or None for other expressions.
    """
    if isinstance(node, ast.Call):
        return last_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def decorator_names(node: Definition) -> list[str]:
    """Return the last segment of every decorator.

    Args:
        node (Definition): Decorated function or class.

    Returns:
        list[str]: For example ``["cached_property"]`` or ``["setter"]``.
    """
    return [name for decorator in node.decorator_list if (name := last_name(decorator))]


def accessor_kind(node: Definition) -> str | None:
    """Tell whether a method is a property accessor.

    Args:
        node (Definition): Function or class.

    Returns:
        str | None: ``property``, ``cached_property``, ``setter``, ``deleter`` or None.
    """
    return next((name for name in decorator_names(node) if name in ACCESSORS), None)


def skip_reason(node: Definition) -> str | None:
    """Explain why a definition must not get its own docstring.

    Args:
        node (Definition): Function or class.

    Returns:
        str | None: The reason, or None when a docstring belongs here.
    """
    if isinstance(node, ast.ClassDef):
        return None
    if "overload" in decorator_names(node):
        return "@overload variants get no docstring (ruff D418), only the implementation"
    kind: str | None = accessor_kind(node)
    if kind in {"setter", "deleter"}:
        return "setters and deleters are not documented: the property is in class Attributes"
    if kind == "property":
        return "a property is documented in the Attributes section of its class"
    return None


def is_api_object(node: Definition) -> bool:
    """Tell whether FastAPI publishes the docstring: endpoint or Pydantic model.

    Args:
        node (Definition): Function or class.

    Returns:
        bool: True for ``@router.get(...)``-style endpoints and BaseModel/BaseSettings subclasses.
    """
    if isinstance(node, ast.ClassDef):
        return any(last_name(base) in PYDANTIC_BASES for base in node.bases)
    return any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr in HTTP_ROUTE_METHODS
        for decorator in node.decorator_list
    )


def _as_sentence(text: str) -> str:
    """End a description with a period, like the other docstring descriptions."""
    stripped: str = text.strip()
    return stripped if stripped.endswith((".", "!", "?")) else f"{stripped}."


def api_description(node: ast.AST | None) -> str | None:
    """Find ``description="..."`` of Field/Path/Query/... inside an annotation or default.

    Args:
        node (ast.AST | None): ``Annotated[int, Path(description="Id")]`` or ``Field(...)``.

    Returns:
        str | None: The description as a sentence, or None.
    """
    if node is None:
        return None
    for call in ast.walk(node):
        if isinstance(call, ast.Call) and last_name(call.func) in API_DESCRIBED_CALLS:
            for keyword in call.keywords:
                value: ast.expr = keyword.value
                if (
                    keyword.arg == "description"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    return _as_sentence(value.value)
    return None


def walk_scope(node: ast.AST) -> Iterator[ast.AST]:
    """Walk a function body without entering nested functions, classes and lambdas.

    Args:
        node (ast.AST): Function or statement.

    Yields:
        ast.AST: Every descendant of ``node`` in the same scope.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, SCOPES):
            continue
        yield child
        yield from walk_scope(child)


def _handler_types(source: str, handler: ast.ExceptHandler) -> list[str]:
    """Exception types caught by ``except A:``, ``except (A, B):`` or ``except A, B:``."""
    if handler.type is None:
        return []
    if isinstance(handler.type, ast.Tuple):
        return [_segment(source, element) for element in handler.type.elts]
    return [_segment(source, handler.type)]


def _raised(source: str, node: ast.Raise, handlers: tuple[ast.ExceptHandler, ...]) -> list[str]:
    """Exception classes raised by one ``raise`` statement."""
    exc: ast.expr | None = node.exc
    if exc is None:
        return _handler_types(source, handlers[-1]) if handlers else []
    if isinstance(exc, ast.Name) and not exc.id[:1].isupper():
        # `raise err`: the class comes from `except X as err`
        for handler in reversed(handlers):
            if handler.name == exc.id:
                return _handler_types(source, handler)
        return []
    target: ast.expr = exc.func if isinstance(exc, ast.Call) else exc
    name: str = _segment(source, target)
    return [name] if name.rsplit(".", 1)[-1][:1].isupper() else []


def _collect_raises(
    source: str,
    node: ast.AST,
    handlers: tuple[ast.ExceptHandler, ...],
    found: list[str],
) -> None:
    """Append raised exception classes in ``node``, tracking the enclosing handlers."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, SCOPES):
            continue
        if isinstance(child, ast.ExceptHandler):
            _collect_raises(source, child, (*handlers, child), found)
            continue
        if isinstance(child, ast.Raise):
            found.extend(_raised(source, child, handlers))
        _collect_raises(source, child, handlers, found)


def raised_exceptions(source: str, function: Function) -> list[str]:
    """List the exceptions a function raises itself, in order, without duplicates.

    Args:
        source (str): Module source.
        function (Function): The function.

    Returns:
        list[str]: Exception classes; nested functions are not included.
    """
    found: list[str] = []
    _collect_raises(source, function, (), found)
    return list(dict.fromkeys(found))


def _generic_args(source: str, annotation: ast.expr | None) -> tuple[str | None, list[str]]:
    """Split ``Name[A, B]`` into the last segment of ``Name`` and the argument texts."""
    if not isinstance(annotation, ast.Subscript):
        return None, []
    inner: ast.expr = annotation.slice
    elements: list[ast.expr] = inner.elts if isinstance(inner, ast.Tuple) else [inner]
    return last_name(annotation.value), [_segment(source, element) for element in elements]


def yield_type(source: str, annotation: ast.expr | None) -> str | None:
    """Return Y of ``Iterator[Y]``, ``Generator[Y, S, R]``, ``AsyncIterator[Y]`` and the like.

    Args:
        source (str): Module source.
        annotation (ast.expr | None): Return annotation.

    Returns:
        str | None: The yielded type, or None when it cannot be told.
    """
    name, args = _generic_args(source, annotation)
    return args[0] if name in YIELDING_GENERICS and args else None


def generator_return_type(source: str, annotation: ast.expr | None) -> str | None:
    """Return R of ``Generator[Y, S, R]`` unless it is None.

    Args:
        source (str): Module source.
        annotation (ast.expr | None): Return annotation.

    Returns:
        str | None: The type a generator returns with ``return value``.
    """
    name, args = _generic_args(source, annotation)
    if name == "Generator" and len(args) >= GENERATOR_ARITY and args[2] != "None":
        return args[2]
    return None


def _head(name: str, type_text: str | None) -> str:
    """Entry head: ``name (type)`` or ``name``."""
    return f"{name} ({type_text})" if type_text else name


def _default_tail(source: str, default: ast.expr | None) -> str | None:
    """``Defaults to `x`.`` for a plain default; FastAPI ``Query(...)`` calls are not defaults."""
    if default is None or isinstance(default, ast.Call):
        return None
    return f"Defaults to `{_segment(source, default)}`."


def _argument_entry(source: str, arg: ast.arg, name: str, default: ast.expr | None) -> Entry:
    """Entry for one function argument."""
    annotation: ast.expr | None = arg.annotation
    type_text: str | None = _segment(source, annotation) if annotation is not None else None
    description: str = (
        api_description(annotation) or api_description(default) or DESCRIPTION_PLACEHOLDER
    )
    return Entry(name, _head(name, type_text), _default_tail(source, default), description)


def argument_entries(source: str, arguments: ast.arguments, *, method: bool) -> list[Entry]:
    """Entries for the documented arguments of a signature.

    The first ``self``/``cls`` of a method and underscore-only names (``_``) are skipped, as ruff
    and pydoclint expect; a plain function documents a parameter named ``cls`` like any other.

    Args:
        source (str): Module source.
        arguments (ast.arguments): The signature.
        method (bool): The function is defined directly in a class body.

    Returns:
        list[Entry]: Args entries in signature order.
    """
    positional: list[ast.arg] = [*arguments.posonlyargs, *arguments.args]
    defaults: list[ast.expr | None] = [
        *([None] * (len(positional) - len(arguments.defaults))),
        *arguments.defaults,
    ]
    items: list[tuple[ast.arg, str, ast.expr | None]] = [
        (arg, arg.arg, default) for arg, default in zip(positional, defaults, strict=True)
    ]
    if arguments.vararg is not None:
        items.append((arguments.vararg, f"*{arguments.vararg.arg}", None))
    items.extend(
        (arg, arg.arg, default)
        for arg, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True)
    )
    if arguments.kwarg is not None:
        items.append((arguments.kwarg, f"**{arguments.kwarg.arg}", None))
    if method and positional and positional[0].arg in {"self", "cls"}:
        items = items[1:]
    return [
        _argument_entry(source, arg, name, default)
        for arg, name, default in items
        if not re.fullmatch(r"\**_+", name)
    ]


def find_init(cls: ast.ClassDef) -> Function | None:
    """Return ``__init__`` defined directly in the class body.

    Args:
        cls (ast.ClassDef): The class.

    Returns:
        Function | None: The constructor, or None.
    """
    return next(
        (node for node in cls.body if isinstance(node, FUNCTIONS) and node.name == "__init__"),
        None,
    )


def _class_flags(cls: ast.ClassDef) -> tuple[bool, bool]:
    """(is an Enum, is a Pydantic model) by the names of the bases."""
    names: list[str] = [name for base in cls.bases if (name := last_name(base))]
    is_enum: bool = any("Enum" in name or "Flag" in name for name in names)
    return is_enum, any(name in PYDANTIC_BASES for name in names)


def _field_entry(source: str, cls: ast.ClassDef, node: ast.AnnAssign | ast.Assign) -> Entry | None:
    """Attributes entry for a public class-level field, constant or enum member."""
    target: ast.expr = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
    if not isinstance(target, ast.Name) or target.id.startswith("_"):
        return None
    annotation: ast.expr | None = node.annotation if isinstance(node, ast.AnnAssign) else None
    type_text: str | None = _segment(source, annotation) if annotation is not None else None
    is_enum, is_pydantic = _class_flags(cls)
    is_classvar: bool = (
        type_text is not None and re.match(r"[\w.]*ClassVar\[", type_text) is not None
    )
    value: ast.expr | None = node.value
    tail: str | None = None
    if value is not None and not is_enum and not is_classvar and isinstance(value, SIMPLE_DEFAULTS):
        tail = f"Defaults to `{_segment(source, value)}`."
    description: str = api_description(value) or api_description(annotation) or ""
    if not description:
        description = (
            "Pydantic model configuration."
            if target.id == "model_config" and is_pydantic
            else DESCRIPTION_PLACEHOLDER
        )
    return Entry(target.id, _head(target.id, type_text), tail, description)


def _setter_names(cls: ast.ClassDef) -> set[str]:
    """Names of properties that have a ``@name.setter``."""
    return {
        decorator.value.id
        for node in cls.body
        if isinstance(node, FUNCTIONS)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Attribute)
        and decorator.attr == "setter"
        and isinstance(decorator.value, ast.Name)
    }


def attribute_entries(source: str, cls: ast.ClassDef) -> list[Entry]:
    """Entries for the Attributes section: what pydoclint checks against the class.

    Public class-level fields, constants, enum members and ``@property`` getters (marked
    ``Read-only.`` without a setter). Private, dunder and ``@cached_property`` members are not
    included: pydoclint does not treat the latter as a class attribute.

    Args:
        source (str): Module source.
        cls (ast.ClassDef): The class.

    Returns:
        list[Entry]: Entries in source order, one per name.
    """
    setters: set[str] = _setter_names(cls)
    entries: dict[str, Entry] = {}
    for node in cls.body:
        entry: Entry | None = None
        if isinstance(node, ast.AnnAssign | ast.Assign) and (
            isinstance(node, ast.AnnAssign) or len(node.targets) == 1
        ):
            entry = _field_entry(source, cls, node)
        elif (
            isinstance(node, FUNCTIONS)
            and accessor_kind(node) == "property"
            and not node.name.startswith("_")
        ):
            returns: str | None = _segment(source, node.returns) if node.returns else None
            tail: str | None = None if node.name in setters else "Read-only."
            entry = Entry(node.name, _head(node.name, returns), tail, DESCRIPTION_PLACEHOLDER)
        if entry is not None and entry.key not in entries:
            entries[entry.key] = entry
    return list(entries.values())


def undeclared_attributes(source: str, cls: ast.ClassDef) -> list[str]:
    """Public ``self.x = ...`` of ``__init__`` that are not declared in the class body.

    pydoclint only accepts class-level attributes in the Attributes section, so these must be
    declared as ``x: T`` in the class body to be documented.

    Args:
        source (str): Module source.
        cls (ast.ClassDef): The class.

    Returns:
        list[str]: Attribute names in assignment order.
    """
    init: Function | None = find_init(cls)
    if init is None:
        return []
    declared: set[str] = {entry.key for entry in attribute_entries(source, cls)}
    names: list[str] = []
    for node in walk_scope(init):
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and not target.attr.startswith("_")
                and target.attr not in declared
                and target.attr not in names
            ):
                names.append(target.attr)
    return names


def _value_sections(source: str, function: Function) -> list[Section]:
    """Returns and Yields sections of a function."""
    sections: list[Section] = []
    returns: str | None = _segment(source, function.returns) if function.returns else None
    yields: bool = any(isinstance(node, ast.Yield | ast.YieldFrom) for node in walk_scope(function))
    returns_entry: Entry | None = None
    if yields:
        if any(isinstance(n, ast.Return) and n.value is not None for n in walk_scope(function)):
            # generator with `return value`: Generator[Y, S, R] -> R; an annotated generator
            # whose R is None (or that is not a Generator) returns nothing worth documenting
            value_type: str | None = generator_return_type(source, function.returns)
            if value_type is not None or function.returns is None:
                returns_entry = Entry(VALUE_KEY, value_type, None, DESCRIPTION_PLACEHOLDER)
    elif returns is not None and returns not in NO_RETURN:
        returns_entry = Entry(VALUE_KEY, returns, None, DESCRIPTION_PLACEHOLDER)
    elif returns is None and any(
        isinstance(node, ast.Return) and node.value is not None for node in walk_scope(function)
    ):
        # no annotation yet, but a value is returned: an untyped Returns entry, so a written
        # Returns section is not dropped while the annotation is missing
        returns_entry = Entry(VALUE_KEY, None, None, DESCRIPTION_PLACEHOLDER)
    if returns_entry is not None:
        sections.append(Section("Returns", (returns_entry,)))
    if yields:
        yielded: str | None = yield_type(source, function.returns)
        sections.append(
            Section("Yields", (Entry(VALUE_KEY, yielded, None, DESCRIPTION_PLACEHOLDER),))
        )
    return sections


def build_model(source: str, node: Definition, *, method: bool) -> Model:
    """Describe what the docstring of a function or class must contain.

    Args:
        source (str): Module source.
        node (Definition): Function or class.
        method (bool): ``node`` is a function defined directly in a class body.

    Returns:
        Model: Managed sections and whether the docstring is published by FastAPI.
    """
    sections: list[Section] = []
    preserved: frozenset[str] = frozenset()

    def add(title: str, entries: list[Entry]) -> None:
        if entries:
            sections.append(Section(title, tuple(entries)))

    if isinstance(node, ast.ClassDef):
        init: Function | None = find_init(node)
        add("Args", argument_entries(source, init.args, method=True) if init else [])
        add("Attributes", attribute_entries(source, node))
        preserved = frozenset(undeclared_attributes(source, node))
    elif "cached_property" not in decorator_names(node):
        # @cached_property is documented like an attribute: one line, no sections
        add("Args", argument_entries(source, node.args, method=method))
        sections.extend(_value_sections(source, node))
        raises: list[str] = raised_exceptions(source, node)
        add("Raises", [Entry(name, name, None, DESCRIPTION_PLACEHOLDER) for name in raises])
    return Model(tuple(sections), is_api_object(node), preserved)
