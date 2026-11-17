from docsync.google import (
    Block,
    ParsedDocstring,
    escape_snippet,
    parse_docstring,
    render_docstring,
    strip_generated_tails,
)
from docsync.model import (
    DESCRIPTION_PLACEHOLDER,
    SUMMARY_PLACEHOLDER,
    VALUE_KEY,
    Entry,
    Model,
    Section,
)

EXISTING: str = '''"""Take a lead.

    Markdown **for** Swagger.

    \\f
    Arguments:
        lead_id (Annotated[int, Path(ge=1)]): Lead number
            from the card.
        gone (str): Removed argument.
        force (bool): Take anyway. Defaults to `False`.

    Returns:
        LeadOut: The taken lead.

    Example:
        >>> take(1)
          continued
    """'''


def _model(*sections: Section, api: bool = False) -> Model:
    return Model(tuple(sections), api)


def _arg(key: str, type_text: str | None = None, tail: str | None = None) -> Entry:
    head: str = f"{key} ({type_text})" if type_text else key
    return Entry(key, head, tail, DESCRIPTION_PLACEHOLDER)


def _descriptions(block: Block) -> dict[str, str]:
    return {key: strip_generated_tails(entry.text()) for key, entry in block.entries.items()}


def _parse(literal: str) -> ParsedDocstring:
    doc: ParsedDocstring | None = parse_docstring(literal)
    assert doc is not None
    return doc


def test_parse_existing_docstring() -> None:
    doc: ParsedDocstring = _parse(EXISTING)
    assert doc.prefix == ""
    assert [(b.kind, b.raw_title) for b in doc.blocks] == [
        ("text", ""),
        ("form_feed", ""),
        ("section", "Arguments"),
        ("section", "Returns"),
        ("section", "Example"),
    ]
    assert doc.blocks[0].lines == ["Take a lead.", "", "Markdown **for** Swagger."]
    assert [s.title for s in doc.sections] == ["Args", "Returns", "Example"]
    assert _descriptions(doc.sections[0]) == {
        "lead_id": "Lead number from the card.",
        "gone": "Removed argument.",
        "force": "Take anyway.",
    }
    assert doc.sections[0].entries["lead_id"].head == "lead_id (Annotated[int, Path(ge=1)])"
    assert _descriptions(doc.sections[1]) == {VALUE_KEY: "The taken lead."}
    assert doc.sections[2].lines == ["    >>> take(1)", "      continued"]


def test_parse_rejects_other_literals_and_reads_summary_on_next_line() -> None:
    assert parse_docstring("'''Single.'''") is None
    assert parse_docstring('"Double."') is None
    doc: ParsedDocstring = _parse('r"""\n    Raw summary.\n    """')
    assert doc.prefix == "r"
    assert [(b.kind, b.lines) for b in doc.blocks] == [("text", ["Raw summary."])]
    assert doc.sections == []


def test_strip_generated_tails() -> None:
    assert strip_generated_tails("Lead id. Read-only.") == "Lead id."
    assert strip_generated_tails("Retries. Defaults to `{'a': 1}`.") == "Retries."
    assert strip_generated_tails("Read-only.") == ""
    # only a whole trailing sentence is a generated tail
    for kept in (
        "Mentions Read-only. in the middle",
        "The value is Read-only.",
        "Generated suffix such as ``Defaults to `3`.`` or ``Read-only.``.",
    ):
        assert strip_generated_tails(kept) == kept


def test_continuations_at_entry_indent_do_not_start_entries() -> None:
    doc: ParsedDocstring = _parse(
        '"""S.\n\nArgs:\n    tree (Module): Parsed module,\n    redirected to its class: see X.\n'
        "    other: Next.\n\nReturns:\n    tuple[int, str] | None: (target, origin when\n"
        '    redirected), or None.\n"""'
    )
    assert _descriptions(doc.sections[0]) == {
        "tree": "Parsed module, redirected to its class: see X.",
        "other": "Next.",
    }
    assert _descriptions(doc.sections[1]) == {
        VALUE_KEY: "(target, origin when redirected), or None."
    }


def test_entry_heads_dotted_names_and_trailing_colons() -> None:
    doc: ParsedDocstring = _parse(
        '"""S.\n\nArgs:\n    a (int): The a.\n    b (dict[str, list[int]]):\n'
        "        Mapping of user ids to their scores.\n\nRaises:\n    ValueError: Bad input.\n"
        '    errors.NotFoundError: No such lead.\n"""'
    )
    assert _descriptions(doc.sections[0]) == {
        "a": "The a.",
        "b": "Mapping of user ids to their scores.",
    }
    assert _descriptions(doc.sections[1]) == {
        "ValueError": "Bad input.",
        "errors.NotFoundError": "No such lead.",
    }


def test_untyped_value_entry_keeps_its_prose() -> None:
    doc: ParsedDocstring = _parse('"""S.\n\nReturns:\n    A mapping: user id to score.\n"""')
    entry = doc.sections[0].entries[VALUE_KEY]
    assert (entry.head, entry.first) == (None, "A mapping: user id to score.")


def test_unknown_sections_and_form_feed_keep_their_place() -> None:
    doc: ParsedDocstring = _parse(
        '"""S.\n\nExample:\n    take(1)\n\n\\f\nArgs:\n    x (int): X.\n\n'
        'Keyword Args:\n    extra: Extra stuff.\n\nUsage:\n    d("fast")\n"""'
    )
    assert [(b.kind, b.raw_title) for b in doc.blocks] == [
        ("text", ""),
        ("section", "Example"),
        ("form_feed", ""),
        ("section", "Args"),
        ("section", "Keyword Args"),
        ("section", "Usage"),
    ]
    model: Model = _model(Section("Args", (_arg("x", "int"), _arg("y", "str"))), api=True)
    assert render_docstring(model, "", 100, existing=doc) == [
        '"""S.',
        "",
        "Example:",
        "    take(1)",
        "",
        "\\f",
        "",
        "Args:",
        "    x (int): X.",
        f"    y (str): {DESCRIPTION_PLACEHOLDER}",
        "",
        "Keyword Args:",
        "    extra: Extra stuff.",
        "",
        "Usage:",
        '    d("fast")',
        '"""',
    ]


def test_update_removes_a_tail_the_regex_cannot_and_is_stable() -> None:
    tail: str = "Defaults to `'`'`."  # a backtick inside the default
    model: Model = _model(Section("Args", (_arg("sep", "str", tail),)))
    source: str = f'"""S.\n\nArgs:\n    sep (str): Separator. {tail}\n"""'
    first: list[str] = render_docstring(model, "", 100, existing=parse_docstring(source))
    assert first[3] == f"    sep (str): Separator. {tail}"
    again: list[str] = render_docstring(model, "", 100, existing=parse_docstring("\n".join(first)))
    assert again == first
    only_tail: str = f'"""S.\n\nArgs:\n    sep (str): {tail}\n"""'
    rendered: list[str] = render_docstring(model, "", 100, existing=parse_docstring(only_tail))
    assert rendered[3] == f"    sep (str): {DESCRIPTION_PLACEHOLDER} {tail}"


def test_render_new_docstring() -> None:
    model: Model = _model(
        Section("Args", (_arg("x", "int", "Defaults to `1`."),)),
        Section("Returns", (Entry(VALUE_KEY, "str", None, DESCRIPTION_PLACEHOLDER),)),
    )
    assert render_docstring(model, "    ", 100) == [
        f'"""{SUMMARY_PLACEHOLDER}',
        "",
        "Args:",
        f"    x (int): {DESCRIPTION_PLACEHOLDER} Defaults to `1`.",
        "",
        "Returns:",
        f"    str: {DESCRIPTION_PLACEHOLDER}",
        '"""',
    ]
    assert render_docstring(_model(), "", 100) == [f'"""{SUMMARY_PLACEHOLDER}"""']


def test_render_api_object_puts_sections_after_form_feed() -> None:
    model: Model = _model(Section("Args", (_arg("x", "int"),)), api=True)
    # a blank line after \f: ruff does not see a section right on the next line
    assert render_docstring(model, "", 100)[:5] == [
        f'"""{SUMMARY_PLACEHOLDER}',
        "",
        "\\f",
        "",
        "Args:",
    ]
    assert render_docstring(_model(api=True), "", 100) == [f'"""{SUMMARY_PLACEHOLDER}"""']


def test_render_update_keeps_human_text() -> None:
    doc: ParsedDocstring | None = parse_docstring(EXISTING)
    model: Model = _model(
        Section(
            "Args",
            (
                _arg("lead_id", "Annotated[int, Path(ge=1)]"),
                _arg("member_id", "str"),
                _arg("force", "bool", "Defaults to `True`."),
            ),
        ),
        Section("Returns", (Entry(VALUE_KEY, "LeadOut", None, DESCRIPTION_PLACEHOLDER),)),
        Section(
            "Raises", (Entry("HTTPException", "HTTPException", None, DESCRIPTION_PLACEHOLDER),)
        ),
        api=True,
    )
    assert render_docstring(model, "    ", 100, existing=doc) == [
        '"""Take a lead.',
        "",
        "Markdown **for** Swagger.",
        "",
        "\\f",
        "",
        "Arguments:",  # the title as written
        "    lead_id (Annotated[int, Path(ge=1)]): Lead number",  # unchanged entry: wrap kept
        "        from the card.",
        f"    member_id (str): {DESCRIPTION_PLACEHOLDER}",
        "    force (bool): Take anyway. Defaults to `True`.",
        "",
        "Returns:",
        "    LeadOut: The taken lead.",
        "",
        "Raises:",
        f"    HTTPException: {DESCRIPTION_PLACEHOLDER}",
        "",
        "Example:",
        "    >>> take(1)",
        "      continued",
        '"""',
    ]


def test_render_wraps_to_line_length() -> None:
    long_text: str = "word " * 30
    doc: ParsedDocstring | None = parse_docstring(f'"""S.\n\nArgs:\n    x (int): {long_text}\n"""')
    model: Model = _model(Section("Args", (_arg("x", "int", "Defaults to `1`."),)))
    lines: list[str] = render_docstring(model, "        ", 60, existing=doc)
    body: list[str] = lines[3:-1]
    assert all(len("        " + line) <= 60 for line in body)
    assert body[0].startswith("    x (int): word")
    assert all(line.startswith("        word") or "Defaults" in line for line in body[1:])
    assert " ".join(" ".join(body).split()).endswith("word Defaults to `1`.")
    assert " ".join(body).count("word") == 30


def test_render_moves_description_under_a_long_head_and_drops_a_huge_tail() -> None:
    head_type: str = "Annotated[bool, Query(description='Include lead events in the response')]"
    huge_tail: str = "Defaults to `" + "x" * 120 + "`."
    model: Model = _model(Section("Args", (_arg("with_history", head_type, huge_tail),)))
    lines: list[str] = render_docstring(model, "    ", 100)
    assert lines[3] == f"    with_history ({head_type}):"
    assert lines[4] == f"        {DESCRIPTION_PLACEHOLDER}"
    assert "Defaults" not in "\n".join(lines)


def test_render_snippet_numbers_fields_and_escapes() -> None:
    model: Model = _model(
        Section(
            "Args", (Entry("price", "price (Money)", "Defaults to `{'$': 1}`.", "Cost in $."),)
        ),
        api=True,
    )
    lines: list[str] = render_docstring(model, "", 100, mode="snippet")
    assert lines[0] == f'"""${{1:{SUMMARY_PLACEHOLDER}}}'
    assert lines[2] == "\\\\f"
    assert lines[5] == "    price (Money): ${2:Cost in \\$.} Defaults to `{'\\$': 1\\}`."


def test_escape_snippet() -> None:
    assert escape_snippet("a\\b$c}d") == "a\\\\b\\$c\\}d"


def _rerender(model: Model, literal: str, indent: str = "", limit: int = 100) -> list[str]:
    return render_docstring(model, indent, limit, existing=_parse(literal))


def test_long_head_description_survives_a_second_update() -> None:
    head_type: str = "Annotated[bool, Query(description='Include lead events in the response')]"
    model: Model = _model(
        Section("Args", (_arg("with_history", head_type, "Defaults to `False`."),))
    )
    source: str = f'"""S.\n\nArgs:\n    with_history ({head_type}): Human text.\n"""'
    first: list[str] = _rerender(model, source, "    ")
    assert first[3:5] == [
        f"    with_history ({head_type}):",
        "        Human text. Defaults to `False`.",
    ]
    assert _rerender(model, "\n".join(first), "    ") == first


def test_markdown_inside_an_entry_is_kept_when_the_type_changes() -> None:
    model: Model = _model(Section("Args", (_arg("mode", "Literal['fast', 'slow']"),)))
    source: str = (
        '"""S.\n\nArgs:\n    mode (str): One of:\n\n        - `fast`: skip checks;\n'
        '        - `slow`: run everything.\n"""'
    )
    assert _rerender(model, source)[2:7] == [
        "Args:",
        "    mode (Literal['fast', 'slow']): One of:",
        "",
        "        - `fast`: skip checks;",
        "        - `slow`: run everything.",
    ]


def test_stale_default_tail_without_a_final_period_is_replaced() -> None:
    model: Model = _model(Section("Args", (_arg("size", "int", "Defaults to `4`."),)))
    source: str = '"""S.\n\nArgs:\n    size (int): Size in `bytes` Defaults to `3`.\n"""'
    assert _rerender(model, source)[3] == "    size (int): Size in `bytes` Defaults to `4`."


def test_star_names_match_entries_written_without_stars() -> None:
    model: Model = _model(Section("Args", (_arg("*args", "int"), _arg("**kwargs", "str"))))
    source: str = '"""S.\n\nArgs:\n    args (int): Positional values.\n    kwargs: Options.\n"""'
    assert _rerender(model, source)[3:5] == [
        "    *args (int): Positional values.",
        "    **kwargs (str): Options.",
    ]


def test_untyped_returns_prose_gets_the_type_without_losing_text() -> None:
    model: Model = _model(
        Section("Returns", (Entry(VALUE_KEY, "dict[str, int]", None, DESCRIPTION_PLACEHOLDER),))
    )
    source: str = '"""S.\n\nReturns:\n    A mapping: user id to score.\n"""'
    assert _rerender(model, source)[3] == "    dict[str, int]: A mapping: user id to score."


def test_preserved_attribute_entries_are_kept() -> None:
    model: Model = Model(
        (Section("Attributes", (_arg("name", "str"),)),), api=False, preserved=frozenset({"size"})
    )
    source: str = (
        '"""S.\n\nAttributes:\n    size (int): Size in centimetres.\n    gone (int): Removed.\n"""'
    )
    assert _rerender(model, source)[2:5] == [
        "Attributes:",
        f"    name (str): {DESCRIPTION_PLACEHOLDER}",
        "    size (int): Size in centimetres.",
    ]


def test_wrap_boundary_is_exact() -> None:
    model: Model = _model(Section("Args", (_arg("x", "int"),)))
    fits: str = "aaaa bbbb cccc dd"  # "    x (int): " + fits is exactly 30 columns
    assert len(f"    x (int): {fits}") == 30
    one_line: list[str] = _rerender(model, f'"""S.\n\nArgs:\n    x (int): {fits}\n"""', limit=30)
    assert one_line[3:-1] == [f"    x (int): {fits}"]
    longer: list[str] = _rerender(model, f'"""S.\n\nArgs:\n    x (int): {fits}d\n"""', limit=30)
    assert longer[3:-1] == ["    x (int): aaaa bbbb cccc", "        ddd"]


def test_wide_characters_count_twice() -> None:
    model: Model = _model(Section("Args", (_arg("x", "int"),)))
    text: str = "漢字漢字 漢字漢字 漢字"  # 12 characters but 22 columns
    lines: list[str] = _rerender(model, f'"""S.\n\nArgs:\n    x (int): {text} x\n"""', limit=30)
    # by width the first line is full at 30 columns; counted by len() everything would fit on it
    assert lines[3:-1] == ["    x (int): 漢字漢字 漢字漢字", "        漢字 x"]


def test_generated_text_is_escaped_for_the_docstring() -> None:
    entry: Entry = Entry("p", "p (str)", 'Defaults to `r"\\d+"`.', 'Use """triple""" quotes.')
    lines: list[str] = render_docstring(_model(Section("Args", (entry,))), "", 100)
    source: str = "def f() -> None:\n" + "\n".join(f"    {line}" for line in lines) + "\n"
    import warnings  # noqa: PLC0415 - only this test needs it

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        compiled = compile(source, "m.py", "exec")
    namespace: dict[str, object] = {}
    exec(compiled, namespace)  # noqa: S102 - our own generated test source
    function: object = namespace["f"]
    doc: str = getattr(function, "__doc__", "") or ""
    assert 'Use """triple""" quotes.' in doc
    assert 'Defaults to `r"\\d+"`.' in doc
