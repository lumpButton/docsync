# docsync

[![CI](https://github.com/lumpButton/docsync/actions/workflows/ci.yml/badge.svg)](https://github.com/lumpButton/docsync/actions/workflows/ci.yml)

**Keep Google-style Python docstrings in sync with the code.** docsync writes docstring
templates where [ruff](https://docs.astral.sh/ruff/) requires a docstring, keeps existing
docstrings up to date when signatures change, and fails CI while a template is left unfilled.

```text
$ docsync fix
app/leads/service.py: +2 templates, 1 updated
1 file(s) changed

$ docsync check
app/leads/service.py:41:26: DSP001 Docstring placeholder: lead_id (int): TODO: description.
app/leads/router.py:18:5: DSP002 Docstring of `take_lead` is out of date with the code; run fix
```

## Why

ruff and [pydoclint](https://github.com/jsh9/pydoclint) can tell you that a docstring is missing
or that it does not match the signature. Nothing writes or updates the docstring for you, and any
text passes the linters, so a half-filled template ships happily. ruff
[does not support plugins](https://docs.astral.sh/ruff/faq/), so this cannot be a ruff rule or a
formatter extension. docsync is a small companion that runs next to ruff:

- **ruff decides where** a docstring is required (`D101`, `D102`, `D103`, `D106`), with your
  configuration: private names, `@override`, per-file ignores and so on are never duplicated.
- **docsync writes and updates** the docstring from the code.
- **`docsync check` fails** on unfilled placeholders and on docstrings that went out of date.

## Install

As a development dependency (Python 3.12+):

```bash
uv add --dev "docsync @ git+https://github.com/lumpButton/docsync"
```

or with pip:

```bash
pip install "docsync @ git+https://github.com/lumpButton/docsync"
```

ruff is installed with it; docsync calls ruff from the same environment.

## Usage

```bash
docsync fix                  # the current directory
docsync fix app tests/x.py   # files and directories
docsync check                # exit 1 on placeholders or outdated docstrings
```

Directories are walked recursively for `*.py`. Hidden directories (`.venv`, `.git`, caches),
`__pycache__`, `node_modules`, `build` and `dist` are skipped unless you name them.

A typical sequence, locally or in CI:

```bash
ruff check --fix && docsync fix && ruff format && docsync check
```

| Code     | Meaning                                                                           | Fails `check` |
|----------|-----------------------------------------------------------------------------------|---------------|
| `DSP001` | A placeholder (`TODO: ...`) is left in a docstring                                | yes           |
| `DSP002` | A docstring is out of date with the code; `docsync fix` updates it                | yes           |
| `DSW001` | Public `self.x` assigned only in `__init__` cannot be documented in `Attributes`  | no            |
| `DSW002` | A body written after `:` on several lines; move it to its own lines by hand       | no            |

Missing docstrings are reported by ruff itself (`D1xx`), not by docsync.

## What it writes

```python
@router.get("/leads/{lead_id}")
async def get_lead(
    lead_id: Annotated[int, Path(ge=1, description="Lead number")],
    *,
    with_history: Annotated[bool, Query(description="Include lead events")] = False,
) -> LeadOut:
    """TODO: summary.

    \f
    Args:
        lead_id (Annotated[int, Path(ge=1, description="Lead number")]): Lead number.
        with_history (Annotated[bool, Query(description="Include lead events")]): Include lead
            events. Defaults to `False`.

    Returns:
        LeadOut: TODO: description.

    Raises:
        HTTPException: TODO: description.
    """
```

- **Args** from the signature, with types exactly as annotated: `*args`, `**kwargs`,
  positional-only and keyword-only parameters; `self`/`cls` of methods and `_` are skipped.
  Simple defaults become ``Defaults to `x`.``.
- **Returns** from the return annotation (not for `None`, `NoReturn`, `Never`). Generators get
  **Yields** with the yielded type, plus **Returns** for `Generator[Y, S, R]` that return a value.
- **Raises** for exceptions the function raises itself. A bare `raise` or `raise err` inside
  `except` resolves to the caught types. Nested functions are not included.
- **Classes**: `Args` from `__init__`; `Attributes` for public class-level fields, constants,
  enum members and `@property` (marked `Read-only.` without a setter). `@cached_property` gets
  its own one-line docstring. Setters, deleters and `@overload` variants are not documented.
- **FastAPI**: endpoints (`@router.get(...)` and friends) and Pydantic models put the sections
  after `\f`, so [FastAPI keeps them out of OpenAPI](https://fastapi.tiangolo.com/advanced/path-operation-advanced-configuration/)
  and only the summary reaches Swagger. `description=` of `Field`, `Path`, `Query`, `Header`,
  `Body`, ... is copied into the docstring.
- Long lines are wrapped to ruff's `line-length`.

## Updating

When the code changes, `docsync fix` rewrites only what the code determines:

- new parameters, fields, properties and exceptions get entries with placeholders;
- entries for things that no longer exist are removed;
- types and generated tails (`Defaults to ...`, `Read-only.`) are refreshed.

Everything a human wrote is kept: the summary and description (Markdown included), `\f`,
entry descriptions, and other sections (`Example:`, `Note:`, `Keyword Args:`, ...) in their
place. An unchanged description keeps its own line breaks; lists and blank lines inside a
description are never reflowed. Summary-only docstrings and docstrings that are not `"""..."""`
are left alone. `docsync fix` is idempotent.

## Recommended ruff and pydoclint setup

docsync follows the Google convention as ruff and pydoclint check it:

```toml
[tool.ruff.lint]
select = ["D", "DOC102", "DOC201", "DOC202", "DOC402", "DOC403", "DOC501", "DOC502"]
ignore = ["D100", "D104", "D105", "D107", "D301"]
preview = true                 # DOC rules are in preview
explicit-preview-rules = true  # ... and only them

[tool.ruff.lint.pydocstyle]
convention = "google"
ignore-decorators = ["property"]  # properties are documented in the class Attributes

[tool.ruff.lint.pydoclint]
ignore-one-line-docstrings = true

[tool.pydoclint]  # types in docstrings must match the annotations (ruff cannot check this)
style = "google"
arg-type-hints-in-docstring = true
check-return-types = true
check-yield-types = true
skip-checking-short-docstrings = true
check-class-attributes = true
treat-property-methods-as-class-attributes = true
```

`D301` is ignored because docstrings of FastAPI endpoints contain `\f`.

## pre-commit

```yaml
repos:
  - repo: https://github.com/lumpButton/docsync
    rev: v0.1.0
    hooks:
      - id: docsync-fix
      - id: docsync-check
```

## Editor integration

Two commands are meant for editors:

- `docsync fix --stdin-filename <path> -` reads a buffer on stdin and prints the fixed source.
  Use it as a formatter on save. `--stdin-filename` lets ruff apply the per-file rules of the
  real path.
- `docsync at --line <n> --stdin-filename <path> -` prints JSON for the definition at a cursor
  line: either `edits` plus an LSP `snippet` with fields for a new docstring, or `edits` that
  update the existing one, plus a `message` and `warnings`.

A Neovim client (LuaSnip for the snippet) fits in a few lines:

```lua
local function run(args)
  local buf = vim.api.nvim_get_current_buf()
  local text = table.concat(vim.api.nvim_buf_get_lines(buf, 0, -1, false), "\n") .. "\n"
  local cmd = vim.list_extend({ "docsync" }, args)
  return vim.system(cmd, { stdin = text, text = true }):wait()
end

vim.keymap.set("n", "<leader>cs", function()
  local name = vim.api.nvim_buf_get_name(0)
  local line = tostring(vim.api.nvim_win_get_cursor(0)[1])
  local result = run({ "at", "--line", line, "--stdin-filename", name, "-" })
  if result.code ~= 0 then return vim.notify(result.stderr, vim.log.levels.ERROR) end
  local r = vim.json.decode(result.stdout, { luanil = { object = true, array = true } })
  table.sort(r.edits, function(a, b) return a.start > b.start end)
  for _, e in ipairs(r.edits) do
    vim.api.nvim_buf_set_lines(0, e.start, e["end"], false, e.lines)
  end
  if r.snippet then
    vim.api.nvim_win_set_cursor(0, { r.snippet.row + 1, r.snippet.column })
    require("luasnip").lsp_expand(r.snippet.body, { pos = { r.snippet.row, r.snippet.column } })
  end
  if r.message then vim.notify("docsync: " .. r.message) end
end)
```

Run `docsync` from the project's virtual environment so that it uses the project's ruff.

## Limitations

- Only Google style and only `"""` docstrings.
- Pydantic models are recognised by a direct `BaseModel`/`BaseSettings` base; a model that
  inherits from your own base class does not get `\f`.
- A hand-written `Defaults to ...` for a default that is a call (`= Query(5)`) is removed:
  docsync only writes default tails for plain values.
- `line-length` is read from `[tool.ruff]` in `pyproject.toml` (88 if not set); `ruff.toml` and
  `extend` are not followed.

## Development

```bash
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check && uv run mypy
uv run docsync check src   # docsync checks its own docstrings
```

## License

[MIT](LICENSE)
