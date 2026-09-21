"""Windows batch launchers.

These cannot be executed on a non-Windows test runner, so what is checked is
everything that can be verified statically and that has actually broken in
practice: line endings, that each launcher invokes a mode ``run_app.py``
really accepts, and that the shared launcher handles the cases a user will
hit -- no Python, an old Python, a failing run.

The mode cross-check matters most: a launcher passing a flag the application
does not understand would look fine here and fail on the user's desktop.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import run_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_LAUNCHERS = {
    "AeroThermalStudio.bat": "--gui",
    "Setup.bat": "setup",
    "Check-Environment.bat": "--check",
    "Run-Demo.bat": "--demo",
    "Run-MCP-Server.bat": "--mcp",
}

SHARED_LAUNCHER = "_launcher.cmd"


def launcher_text(name: str) -> str:
    """Read a launcher as text."""
    return (PROJECT_ROOT / name).read_text(encoding="ascii")


@pytest.mark.parametrize("name", sorted(EXPECTED_LAUNCHERS))
def test_launcher_exists(name):
    """Every advertised launcher is shipped."""
    assert (PROJECT_ROOT / name).is_file(), f"{name} is missing"


def test_shared_launcher_exists():
    """The batch files delegate to one shared script."""
    assert (PROJECT_ROOT / SHARED_LAUNCHER).is_file()


def test_test_runner_launcher_exists():
    """A launcher for running the suite is provided too."""
    assert (PROJECT_ROOT / "Run-Tests.bat").is_file()


@pytest.mark.parametrize(
    "name", sorted(EXPECTED_LAUNCHERS) + [SHARED_LAUNCHER, "Run-Tests.bat"]
)
def test_launchers_use_windows_line_endings(name):
    """cmd.exe mis-parses labels and goto targets in an LF-only file."""
    raw = (PROJECT_ROOT / name).read_bytes()
    assert b"\r\n" in raw, f"{name} has no CRLF endings"
    # No stray lone LF: every newline must be part of a CRLF pair.
    assert raw.count(b"\n") == raw.count(b"\r\n"), f"{name} mixes line endings"


@pytest.mark.parametrize(
    "name", sorted(EXPECTED_LAUNCHERS) + [SHARED_LAUNCHER, "Run-Tests.bat"]
)
def test_launchers_are_plain_ascii(name):
    """Batch files are read in the console codepage; keep them ASCII."""
    (PROJECT_ROOT / name).read_text(encoding="ascii")


@pytest.mark.parametrize("name,mode", sorted(EXPECTED_LAUNCHERS.items()))
def test_launcher_sets_its_mode(name, mode):
    """Each launcher declares the mode it wants."""
    text = launcher_text(name)
    assert f'set "ATS_MODE={mode}"' in text, f"{name} does not set mode {mode}"


@pytest.mark.parametrize("name", sorted(EXPECTED_LAUNCHERS))
def test_launcher_delegates_to_the_shared_script(name):
    """Python discovery lives in one place, not copied into each file."""
    assert "_launcher.cmd" in launcher_text(name)


@pytest.mark.parametrize(
    "mode", [value for value in EXPECTED_LAUNCHERS.values() if value != "setup"]
)
def test_launcher_modes_are_accepted_by_the_application(mode):
    """A launcher must not pass a flag run_app.py does not understand.

    This is the cross-check that catches a renamed flag before the user does.
    """
    parser = run_app.build_parser()
    parsed = parser.parse_args([mode])
    attribute = mode.lstrip("-").replace("-", "_")
    assert getattr(parsed, attribute) is True


def test_setup_launcher_runs_the_setup_script():
    """Setup.bat invokes setup_env.py rather than run_app.py."""
    shared = launcher_text(SHARED_LAUNCHER)
    assert 'if "%ATS_MODE%"=="setup"' in shared
    assert "setup_env.py" in shared
    assert (PROJECT_ROOT / "setup_env.py").is_file()


def test_shared_launcher_searches_for_python_in_order():
    """A virtual environment wins, then the py launcher, then PATH."""
    text = launcher_text(SHARED_LAUNCHER)
    venv = text.index(".venv")
    py_launcher = text.index("py -3 --version")
    path_python = text.index("python --version")
    assert venv < py_launcher < path_python


def test_shared_launcher_checks_the_python_version():
    """An old interpreter is reported clearly instead of failing on import."""
    text = launcher_text(SHARED_LAUNCHER)
    assert "version_info >= (3, 11)" in text
    assert "too old" in text.lower()


def test_shared_launcher_explains_a_missing_python():
    """The commonest first-run failure gets actionable instructions."""
    text = launcher_text(SHARED_LAUNCHER)
    assert "python.org" in text
    assert "Setup.bat" in text


def test_shared_launcher_runs_from_its_own_directory():
    """Double-clicking from Explorer must not depend on the working directory."""
    text = launcher_text(SHARED_LAUNCHER)
    assert "%~dp0" in text
    assert "pushd" in text and "popd" in text


def test_no_pause_can_run_in_mcp_mode():
    """The MCP server speaks on stdio; any pause would hang its client.

    Including the startup-failure pauses: an AI client that launched the
    server has no way to answer "Press any key".
    """
    shared = launcher_text(SHARED_LAUNCHER)
    assert ":pause_unless_mcp" in shared
    assert 'if "%ATS_MODE%"=="--mcp" goto :eof' in shared

    # Every bare pause must be inside the guarded subroutine, which is the
    # last thing in the file.
    subroutine = shared.index(":pause_unless_mcp\nif")
    bare_pauses = [
        match.start()
        for match in re.finditer(r"^\s*pause\s*$", shared, re.MULTILINE)
    ]
    assert bare_pauses, "the launcher should pause somewhere"
    assert all(position > subroutine for position in bare_pauses), (
        "a pause outside the MCP-guarded subroutine would hang an AI client"
    )


def test_interactive_launchers_pause_on_failure():
    """A person who double-clicked must be able to read the error."""
    shared = launcher_text(SHARED_LAUNCHER)
    assert "call :pause_unless_mcp" in shared


def test_launchers_forward_extra_arguments():
    """Advanced users can still pass flags through."""
    for name in EXPECTED_LAUNCHERS:
        assert 'set "ATS_ARGS=%*"' in launcher_text(name)


def test_gitattributes_protects_the_line_endings():
    """Git must not normalise CRLF out of the launchers on checkout."""
    text = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"\*\.bat\s+text\s+eol=crlf", text)
    assert re.search(r"\*\.cmd\s+text\s+eol=crlf", text)


def test_run_app_modes_are_mutually_exclusive():
    """Two modes at once is a mistake worth catching at the command line."""
    parser = run_app.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--gui", "--mcp"])
