import io
import json
import shutil
from pathlib import Path

import pytest

from docsync.cli import main

SAMPLE: Path = Path(__file__).parent / "project"
CODE: str = "def send(chat_id: int) -> None:\n    pass\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A copy of the sample project config with an empty app package."""
    shutil.copy(SAMPLE / "pyproject.toml", tmp_path / "pyproject.toml")
    (tmp_path / "app").mkdir()
    return tmp_path


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.startswith("docsync ")


def test_fix_files_then_check(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    module: Path = project / "app" / "send.py"
    module.write_text(CODE, encoding="utf-8")

    assert main(["check", "app"]) == 0  # missing docstrings are ruff's D103, not this check
    assert capsys.readouterr().out == ""

    assert main(["fix", "app"]) == 0
    assert "1 file(s) changed" in capsys.readouterr().out
    fixed: str = module.read_text(encoding="utf-8")
    assert '"""TODO: summary.' in fixed
    assert "chat_id (int): TODO: description." in fixed

    assert main(["check", "app"]) == 1
    out: list[str] = capsys.readouterr().out.splitlines()
    assert len(out) == 2
    assert all(" DSP001 " in line for line in out)

    module.write_text(
        fixed.replace("TODO: summary.", "Send a card.").replace("TODO: description.", "Chat.")
    )
    assert main(["check", "app"]) == 0
    assert main(["fix", "app"]) == 0
    assert "0 file(s) changed" in capsys.readouterr().out


def test_check_reports_outdated_docstrings(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    outdated: str = (
        'def send(chat_id: int) -> None:\n    """Send.\n\n    Args:\n        old (int): Old.\n'
        '    """\n'
    )
    (project / "app" / "send.py").write_text(outdated)
    assert main(["check", "app"]) == 1
    out: str = capsys.readouterr().out
    assert " DSP002 " in out
    assert " DSP001 " not in out


def test_fix_walks_the_current_directory_by_default_and_skips_service_dirs(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    skipped: list[Path] = [
        project / ".venv" / "lib" / "dep.py",
        project / ".mypy_cache" / "x.py",
        project / "app" / "__pycache__" / "x.py",
        project / "node_modules" / "pkg" / "x.py",
        project / "build" / "lib" / "x.py",
        project / "dist" / "x.py",
    ]
    for module in skipped:
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(CODE, encoding="utf-8")
    top: Path = project / "top.py"
    top.write_text(CODE, encoding="utf-8")
    (project / "app" / "box.py").write_text(
        "class Box:\n    def __init__(self, size: int) -> None:\n        self.size = size\n"
    )

    assert main(["fix"]) == 0  # no paths: the current directory
    captured = capsys.readouterr()
    assert all(module.read_text(encoding="utf-8") == CODE for module in skipped)
    assert '"""TODO: summary.' in top.read_text(encoding="utf-8")
    assert "2 file(s) changed" in captured.out
    assert " DSW001 " in captured.err
    assert "size" in captured.err

    assert main(["check"]) == 1  # the placeholders just added, found from the current directory
    assert " DSP001 " in capsys.readouterr().out

    # a hidden directory named explicitly is still processed
    assert main(["fix", ".venv"]) == 0
    assert '"""TODO: summary.' in skipped[0].read_text(encoding="utf-8")


def test_fix_stdin_respects_per_file_ignores(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    (project / "tests").mkdir()
    monkeypatch.setattr("sys.stdin", io.StringIO(CODE))
    assert main(["fix", "--stdin-filename", "tests/test_send.py", "-"]) == 0
    assert capsys.readouterr().out == CODE  # ruff ignores D in tests/: nothing to insert


def test_fix_stdin(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    monkeypatch.setattr("sys.stdin", io.StringIO(CODE))
    assert main(["fix", "--stdin-filename", "app/send.py", "-"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith('def send(chat_id: int) -> None:\n    """TODO: summary.')
    assert "pass" not in captured.out


def test_at_prints_json(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    monkeypatch.setattr("sys.stdin", io.StringIO(CODE))
    assert main(["at", "--line", "2", "--stdin-filename", "app/send.py", "-"]) == 0
    response: dict[str, object] = json.loads(capsys.readouterr().out)
    assert response["message"] is None
    assert isinstance(response["snippet"], dict)
    assert response["snippet"]["row"] == 1
