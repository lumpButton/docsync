"""Parse and render Google-style docstrings.

A docstring is kept as blocks in source order: free text (summary, description, Markdown),
``\\f`` lines and sections. Rendering rebuilds only the managed sections (Args, Attributes,
Returns, Yields, Raises) from the model and leaves every other block where and as it was.
Inside managed sections a human description is kept verbatim unless its entry head or generated
tail changed or a line got too long; structured descriptions (lists, blank lines, deeper
indentation) are never reflowed.
"""

import ast
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from docsync.model import SUMMARY_PLACEHOLDER, VALUE_KEY, Entry, Model

type Mode = Literal["plain", "snippet"]
type BlockKind = Literal["text", "form_feed", "section"]

MANAGED_ORDER: tuple[str, ...] = ("Args", "Attributes", "Returns", "Yields", "Raises")
SECTION_ALIASES: dict[str, str] = {
    "Arguments": "Args",
    "Return": "Returns",
    "Yield": "Yields",
    "Raise": "Raises",
}
KNOWN_SECTIONS: frozenset[str] = frozenset(
    {
        *MANAGED_ORDER,
        *SECTION_ALIASES,
        "Example",
        "Examples",
        "Note",
        "Notes",
        "Warning",
        "Warnings",
        "See Also",
        "Todo",
        "References",
        "Methods",
        "Keyword Args",
        "Keyword Arguments",
        "Other Parameters",
    }
)
VALUE_SECTIONS: frozenset[str] = frozenset({"Returns", "Yields"})
FORM_FEED: str = "\\f"  # the two source characters FastAPI cuts OpenAPI descriptions at
ENTRY_INDENT: int = 4
CONTINUATION_INDENT: int = 8
DOCSTRING: re.Pattern[str] = re.compile(r'(?s)([rRuU]?)"""(.*)"""')
HEADER: re.Pattern[str] = re.compile(r"([A-Z][A-Za-z]*(?: [A-Za-z]+){0,2}):\s*")
ENTRY_NAME: re.Pattern[str] = re.compile(r"(\**)([A-Za-z_][\w.]*)")
LIST_ITEM: re.Pattern[str] = re.compile(r"(?:[-*+]|\d+[.)])\s")
READ_ONLY_TAIL: re.Pattern[str] = re.compile(r"(?:^|(?<=[.!?]))\s*Read-only\.$")
DEFAULT_TAIL: re.Pattern[str] = re.compile(r"(?:^|(?<=[.!?\s]))\s*Defaults to `[^`]*`\.$")


@dataclass(slots=True)
class KeptEntry:
    """An entry of an existing managed section, as written.

    Attributes:
        key (str): Name without leading ``*``, or the Returns/Yields value key.
        head (str | None): Text before the colon as written; None for an untyped value entry.
        first (str): Description on the head line.
        rest (list[str]): Following lines as written, relative to the section body.
    """

    key: str
    head: str | None
    first: str
    rest: list[str] = field(default_factory=list)

    def text(self) -> str:
        """Join the description into one line.

        Returns:
            str: The whole description, tails included.
        """
        return " ".join([self.first, *(line.strip() for line in self.rest)]).strip()

    def structured(self) -> bool:
        """Tell whether the description has a layout that must not be reflowed.

        Returns:
            bool: True for blank lines, deeper indentation or list items.
        """
        if LIST_ITEM.match(self.first):
            return True
        return any(
            not line.strip() or _indent(line) > CONTINUATION_INDENT or LIST_ITEM.match(line.strip())
            for line in self.rest
        )


@dataclass(slots=True)
class Block:
    """A part of a docstring: free text, a ``\\f`` line or a section.

    Attributes:
        kind (BlockKind): ``text``, ``form_feed`` or ``section``.
        lines (list[str]): Text lines, or the section body relative to the docstring.
        title (str): Canonical section title (``Arguments`` becomes ``Args``). Defaults to `""`.
        raw_title (str): Section title as written. Defaults to `""`.
        entries (dict[str, KeptEntry]): Entries of a managed section by key.
    """

    kind: BlockKind
    lines: list[str] = field(default_factory=list)
    title: str = ""
    raw_title: str = ""
    entries: dict[str, KeptEntry] = field(default_factory=dict)


@dataclass(slots=True)
class ParsedDocstring:
    """An existing ``\"\"\"...\"\"\"`` docstring.

    Attributes:
        prefix (str): String prefix such as ``r``; empty for a plain string.
        blocks (list[Block]): Blocks in source order.
        sections (list[Block]): Section blocks in source order. Read-only.
    """

    prefix: str
    blocks: list[Block]

    @property
    def sections(self) -> list[Block]:
        return [block for block in self.blocks if block.kind == "section"]


@dataclass(frozen=True, slots=True)
class _Token:
    text: str
    width: int


def _indent(line: str) -> int:
    """Number of leading spaces."""
    return len(line) - len(line.lstrip())


def display_width(text: str) -> int:
    """Width of text in terminal columns (wide East Asian characters count twice).

    Args:
        text (str): Text without newlines.

    Returns:
        int: Number of columns.
    """
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def strip_generated_tails(description: str) -> str:
    """Remove ``Read-only.`` and ``Defaults to `...`.`` that the generator appends.

    ``Read-only.`` counts only as a whole sentence; ``Defaults to `x`.`` also after a word, so a
    stale tail behind a description without a final period is still recognised.

    Args:
        description (str): Entry description as written.

    Returns:
        str: The human part of the description.
    """
    text: str = READ_ONLY_TAIL.sub("", description)
    text = DEFAULT_TAIL.sub("", text)
    return text.strip()


def _entry_colon(text: str) -> int | None:
    """Index of the colon that ends an entry head; brackets of the type are skipped."""
    depth: int = 0
    for index, char in enumerate(text):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == ":" and depth == 0:
            return index
    return None


def _is_type(text: str) -> bool:
    """Whether text before a Returns/Yields colon is a type expression, not prose."""
    try:
        ast.parse(text, mode="eval")
    except SyntaxError:
        return False
    return True


def entry_start(title: str, text: str) -> KeptEntry | None:
    """Read the head of an entry: ``name: ...``, ``name (type): ...`` or ``type: ...``.

    Args:
        title (str): Canonical section title.
        text (str): A stripped line at the entry indentation.

    Returns:
        KeptEntry | None: The entry without continuation lines, or None for a line that is not an
        entry head (a continuation written at the entry indentation).
    """
    colon: int | None = _entry_colon(text)
    if title in VALUE_SECTIONS:
        head: str = text[:colon].strip() if colon is not None else ""
        if colon is not None and head and _is_type(head):
            return KeptEntry(VALUE_KEY, head, text[colon + 1 :].strip())
        return KeptEntry(VALUE_KEY, None, text)
    match: re.Match[str] | None = ENTRY_NAME.match(text)
    if match is None or colon is None:
        return None
    between: str = text[match.end() : colon].strip()
    if between and not (between.startswith("(") and between.endswith(")")):
        return None
    return KeptEntry(match.group(2), text[:colon].strip(), text[colon + 1 :].strip())


def parse_entries(title: str, lines: list[str]) -> dict[str, KeptEntry]:
    """Read the entries of a managed section.

    Args:
        title (str): Canonical section title.
        lines (list[str]): Section body: entries at 4 spaces, continuations deeper.

    Returns:
        dict[str, KeptEntry]: Entries by key, as written.
    """
    entries: dict[str, KeptEntry] = {}
    current: KeptEntry | None = None
    for line in lines:
        started: KeptEntry | None = None
        # Returns/Yields hold a single entry: everything after its first line continues it
        if (
            line.strip()
            and _indent(line) <= ENTRY_INDENT
            and not (title in VALUE_SECTIONS and current)
        ):
            started = entry_start(title, line.strip())
        if started is not None:
            current = started
            entries.setdefault(started.key, started)
        elif current is not None:
            current.rest.append(line)
    for entry in entries.values():
        while entry.rest and not entry.rest[-1].strip():
            entry.rest.pop()
    return entries


def _trim_blank_tail(lines: list[str]) -> list[str]:
    """Drop trailing blank lines in place and return the list."""
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _dedent(raw: list[str]) -> list[str]:
    """First line as is, the rest without their common indentation."""
    widths: list[int] = [_indent(line) for line in raw[1:] if line.strip()]
    common: int = min(widths, default=0)
    return [raw[0].strip(), *(line[common:] for line in raw[1:])]


def _section_title(lines: list[str], index: int) -> str | None:
    """Section title at a line: a known one, or a short ``Title:`` over an indented body."""
    header: re.Match[str] | None = HEADER.fullmatch(lines[index])
    if header is None:
        return None
    title: str = header.group(1)
    if title in KNOWN_SECTIONS:
        return title
    following: list[str] = [line for line in lines[index + 1 :] if line.strip()]
    return title if following and _indent(following[0]) > 0 else None


def parse_docstring(literal: str) -> ParsedDocstring | None:
    """Parse the source text of a docstring literal into blocks.

    Args:
        literal (str): Source of the string literal, with prefix and quotes.

    Returns:
        ParsedDocstring | None: None unless it is a ``\"\"\"...\"\"\"`` string.
    """
    match: re.Match[str] | None = DOCSTRING.fullmatch(literal)
    if match is None:
        return None
    lines: list[str] = _dedent(match.group(2).split("\n"))
    if not lines[0]:
        lines = lines[1:]  # summary on the line after the quotes
    _trim_blank_tail(lines)
    blocks: list[Block] = []
    current: Block | None = None
    for index, line in enumerate(lines):
        if line.strip() == FORM_FEED and _indent(line) == 0:
            blocks.append(Block("form_feed"))
            current = None
            continue
        title: str | None = _section_title(lines, index) if _indent(line) == 0 else None
        if title is not None:
            current = Block("section", title=SECTION_ALIASES.get(title, title), raw_title=title)
            blocks.append(current)
        elif current is None or (current.kind == "section" and line.strip() and _indent(line) == 0):
            current = Block("text", [line])  # free text, also after a section ends
            blocks.append(current)
        else:
            current.lines.append(line)
    for block in blocks:
        _trim_blank_tail(block.lines)
        if block.kind == "section" and block.title in MANAGED_ORDER:
            block.entries = parse_entries(block.title, block.lines)
    blocks = [b for b in blocks if b.kind != "text" or any(line.strip() for line in b.lines)]
    return ParsedDocstring(match.group(1), blocks)


def _wrap(head: str | None, head_width: int, tokens: list[_Token], limit: int) -> list[str]:
    """Greedy word wrap: ``    head: words``, continuation lines indented by 8."""
    lines: list[str] = []
    if head is not None:
        line, width, empty = f"{' ' * ENTRY_INDENT}{head}:", ENTRY_INDENT + head_width + 1, False
    else:
        line, width, empty = " " * ENTRY_INDENT, ENTRY_INDENT, True
    for token in tokens:
        need: int = width + token.width if empty else width + 1 + token.width
        if need <= limit or empty:
            line = line + token.text if empty else f"{line} {token.text}"
            width, empty = need, False
        else:
            lines.append(line)
            line = " " * CONTINUATION_INDENT + token.text
            width = CONTINUATION_INDENT + token.width
    lines.append(line)
    return lines


def _words(text: str) -> list[_Token]:
    """Split text into wrappable words."""
    return [_Token(word, display_width(word)) for word in text.split()]


def escape_snippet(text: str) -> str:
    """Escape text for an LSP snippet body.

    Args:
        text (str): Plain text.

    Returns:
        str: Text with ``\\``, ``$`` and ``}`` escaped.
    """
    return text.replace("\\", "\\\\").replace("$", "\\$").replace("}", "\\}")


def escape_docstring(text: str) -> str:
    """Make generated text safe inside a non-raw ``\"\"\"`` docstring.

    Args:
        text (str): Text taken from code (a default value, a ``description=``, a type).

    Returns:
        str: Text with backslashes doubled and triple quotes broken up.
    """
    return text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


@dataclass(slots=True)
class _Renderer:
    """Builds the lines of managed sections; snippet mode turns new text into numbered fields."""

    mode: Mode
    limit: int
    raw: bool
    fields: int = 0

    def gen(self, text: str) -> str:
        """Generated text as it must appear in the source (docstring and snippet escaping)."""
        safe: str = text if self.raw else escape_docstring(text)
        return escape_snippet(safe) if self.mode == "snippet" else safe

    def new_text(self, default: str) -> list[_Token]:
        if self.mode == "snippet":
            self.fields += 1
            text: str = f"${{{self.fields}:{self.gen(default)}}}"
            return [_Token(text, display_width(default))]
        return _words(self.gen(default))

    def tail(self, entry: Entry) -> str | None:
        # a tail that does not fit even on a continuation line is left out
        if entry.tail and CONTINUATION_INDENT + len(entry.tail) <= self.limit:
            return self.gen(entry.tail)
        return None

    def new_entry(self, entry: Entry) -> list[str]:
        tokens: list[_Token] = self.new_text(entry.description)
        tail: str | None = self.tail(entry)
        if tail:
            tokens.extend(_words(tail))
        head: str | None = self.gen(entry.head) if entry.head else None
        return _wrap(head, display_width(entry.head or ""), tokens, self.limit)

    def kept_entry(self, entry: Entry, kept: KeptEntry) -> list[str]:
        head: str | None = self.gen(entry.head) if entry.head else None
        tail: str | None = self.tail(entry)
        written: str = kept.text()
        human: str = strip_generated_tails(written)
        if tail and human.endswith(tail):
            human = human.removesuffix(tail).rstrip()  # a tail the regex cannot strip
        if not human:
            return self.new_entry(entry)
        old_tail: str = written.removeprefix(human).strip()
        if kept.structured():
            return self._structured(head, tail, kept, old_tail)
        original: list[str] = _original_lines(kept)
        unchanged: bool = kept.head == head and old_tail == (tail or "")
        if unchanged and all(display_width(line) <= self.limit for line in original):
            return original  # hand wrapping is respected
        tokens: list[_Token] = _words(human) + (_words(tail) if tail else [])
        return _wrap(head, display_width(head or ""), tokens, self.limit)

    def _structured(
        self, head: str | None, tail: str | None, kept: KeptEntry, old_tail: str
    ) -> list[str]:
        """Keep a laid-out description verbatim; only the head and the tail change."""
        first: str = kept.first
        rest: list[str] = list(kept.rest)
        if old_tail:
            if rest:
                rest[-1] = rest[-1].rstrip().removesuffix(old_tail).rstrip()
            else:
                first = first.removesuffix(old_tail).rstrip()
        lead: str = f"{' ' * ENTRY_INDENT}{head}:" if head is not None else " " * ENTRY_INDENT
        lines: list[str] = [f"{lead} {first}".rstrip() if head is not None else f"{lead}{first}"]
        lines.extend(_trim_blank_tail(rest))
        if tail:
            if display_width(lines[-1]) + 1 + display_width(tail) <= self.limit:
                lines[-1] = f"{lines[-1]} {tail}"
            else:
                lines.append(" " * CONTINUATION_INDENT + tail)
        return lines


def _original_lines(kept: KeptEntry) -> list[str]:
    """The entry as it was written."""
    lead: str = " " * ENTRY_INDENT + (f"{kept.head}:" if kept.head is not None else "")
    first: str = f"{lead} {kept.first}".rstrip() if kept.head is not None else lead + kept.first
    return [first, *kept.rest]


def _managed_block(
    render: _Renderer, title: str, entries: tuple[Entry, ...], old: Block | None, extra: list[str]
) -> Block:
    """Build one managed section: model entries, plus kept entries named in ``extra``."""
    kept: dict[str, KeptEntry] = old.entries if old else {}
    by_name: dict[str, KeptEntry] = {key.lstrip("*"): entry for key, entry in kept.items()}
    lines: list[str] = []
    for entry in entries:
        found: KeptEntry | None = by_name.get(entry.key.lstrip("*"))
        lines.extend(render.kept_entry(entry, found) if found else render.new_entry(entry))
    for name in extra:
        if name in by_name and name not in {e.key.lstrip("*") for e in entries}:
            lines.extend(_original_lines(by_name[name]))
    raw_title: str = old.raw_title if old and old.title == title else title
    return Block("section", lines, title, raw_title)


def _join(blocks: list[Block]) -> list[str]:
    """Blocks to lines: one blank line between blocks, ``\\f`` included."""
    out: list[str] = []
    previous: Block | None = None
    for block in blocks:
        # also after \f: ruff does not see a section that starts right on the next line
        # (DOC201 then reports a documented return as missing)
        if previous is not None:
            out.append("")
        if block.kind == "form_feed":
            out.append(FORM_FEED)
        elif block.kind == "section":
            out.append(f"{block.raw_title}:")
            out.extend(block.lines)
        else:
            out.extend(block.lines)
        previous = block
    return out


def render_docstring(
    model: Model,
    indent: str,
    line_length: int,
    mode: Mode = "plain",
    existing: ParsedDocstring | None = None,
) -> list[str]:
    """Render a docstring from the model, keeping what an existing docstring already says.

    Args:
        model (Model): What the docstring must contain.
        indent (str): Indentation of the docstring in the file.
        line_length (int): Maximum line length (ruff ``line-length``).
        mode (Mode): ``plain`` text or an LSP ``snippet`` with fields for new text. Defaults to
            `"plain"`.
        existing (ParsedDocstring | None): Docstring to update, or None for a new one. Defaults to
            `None`.

    Returns:
        list[str]: Lines without the common indentation; the first one has the opening quotes.
    """
    prefix: str = existing.prefix if existing else ""
    render: _Renderer = _Renderer(mode, line_length - len(indent), raw="r" in prefix.lower())
    if existing is not None:
        blocks: list[Block] = list(existing.blocks)
    else:
        summary: str = " ".join(token.text for token in render.new_text(SUMMARY_PLACEHOLDER))
        blocks = [Block("text", [summary])]

    old: dict[str, Block] = {b.title: b for b in blocks if b.kind == "section"}
    managed: list[Block] = []
    for title in MANAGED_ORDER:
        entries: tuple[Entry, ...] = next(
            (s.entries for s in model.sections if s.title == title), ()
        )
        extra: list[str] = sorted(model.preserved) if title == "Attributes" else []
        block: Block = _managed_block(render, title, entries, old.get(title), extra)
        if block.lines:
            managed.append(block)

    positions: list[int] = [
        i for i, b in enumerate(blocks) if b.kind == "section" and b.title in MANAGED_ORDER
    ]
    at: int = positions[0] if positions else len(blocks)
    rest: list[Block] = [b for i, b in enumerate(blocks) if i not in positions]
    at -= sum(1 for i in positions if i < at)
    if model.api and managed and not any(b.kind == "form_feed" for b in rest[:at]):
        # FastAPI endpoint / Pydantic model: sections after \f stay out of OpenAPI
        managed.insert(0, Block("form_feed"))
    lines: list[str] = _join([*rest[:at], *managed, *rest[at:]])

    if mode == "snippet":
        lines = [escape_snippet(line) if line == FORM_FEED else line for line in lines]
    lines[0] = f'{prefix}"""{lines[0]}'
    if len(lines) == 1:
        lines[0] += '"""'
    else:
        lines.append('"""')
    return lines
