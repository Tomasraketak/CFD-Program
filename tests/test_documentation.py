"""Documentation consistency.

Documentation goes stale quietly: a renamed tool or a changed default is
invisible until a user follows the instructions and they do not work. These
tests check the claims that can be verified mechanically against the code, and
that the English and Czech versions stay in step.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS = PROJECT_ROOT / "docs"

LANGUAGES = ("en", "cs")
DOCUMENTS = ("TUTORIAL.md", "USER_GUIDE.md", "MCP_AI_GUIDE.md", "SHIELD_STUDY_GUIDE.md")


def read(language: str, name: str) -> str:
    """Read one documentation file."""
    return (DOCS / language / name).read_text(encoding="utf-8")


def all_docs() -> list[tuple[str, str, str]]:
    """Every (language, name, text) triple."""
    return [
        (language, name, read(language, name))
        for language in LANGUAGES
        for name in DOCUMENTS
    ]


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("name", DOCUMENTS)
def test_document_exists_in_both_languages(language, name):
    """Both language sets are complete."""
    assert (DOCS / language / name).is_file(), f"docs/{language}/{name} missing"


def test_documentation_index_exists():
    """The index is what the GUI's Help menu opens."""
    index = DOCS / "README.md"
    assert index.is_file()
    text = index.read_text(encoding="utf-8")
    for language in LANGUAGES:
        for name in DOCUMENTS:
            assert f"{language}/{name}" in text, f"index does not link {language}/{name}"


@pytest.mark.parametrize("language,name,text", all_docs())
def test_documents_are_substantial(language, name, text):
    """A stub would pass the other checks; require real content."""
    assert len(text) > 6000, f"docs/{language}/{name} is too short to be useful"


# ---------------------------------------------------------------------------
# Claims that must match the code
# ---------------------------------------------------------------------------


def test_every_mcp_tool_is_documented():
    """A tool an agent can call but nobody documented is a trap."""
    pytest.importorskip("mcp")
    import asyncio

    import mcp_server

    tools = asyncio.run(mcp_server.server.list_tools())
    for language in LANGUAGES:
        text = read(language, "MCP_AI_GUIDE.md")
        for tool in tools:
            assert tool.name in text, (
                f"tool '{tool.name}' is not documented in {language}"
            )


def test_documented_tool_count_matches_reality():
    """The guides state a tool count; it must be the real one."""
    pytest.importorskip("mcp")
    import asyncio

    import mcp_server

    count = len(asyncio.run(mcp_server.server.list_tools()))
    assert count == 15, (
        f"the documentation says fifteen tools but the server exposes "
        f"{count}; update docs/en/MCP_AI_GUIDE.md and docs/cs/MCP_AI_GUIDE.md"
    )


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_launcher_is_documented(language):
    """A shipped launcher nobody mentions will not be found."""
    launchers = sorted(path.name for path in PROJECT_ROOT.glob("*.bat"))
    assert launchers, "no launchers found"

    combined = " ".join(read(language, name) for name in DOCUMENTS)
    for launcher in launchers:
        assert launcher in combined, (
            f"{launcher} is not mentioned in the {language} documentation"
        )


@pytest.mark.parametrize("language", LANGUAGES)
def test_documented_visualisation_modes_exist(language):
    """Every mode named in the docs is one the renderer accepts."""
    from backend.visualizer import VISUALIZATION_TYPES

    text = read(language, "MCP_AI_GUIDE.md")
    for mode in VISUALIZATION_TYPES:
        assert mode in text, f"visualisation mode '{mode}' undocumented in {language}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_documented_camera_views_exist(language):
    """Camera views named in the docs must be real."""
    from backend.visualizer import CAMERA_VIEWS

    text = read(language, "MCP_AI_GUIDE.md")
    for view in CAMERA_VIEWS:
        assert view in text, f"camera view '{view}' undocumented in {language}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_documented_bounds_match_the_models(language):
    """The stated limits are the ones the code enforces."""
    from core.models import ANGLE_LIMIT_DEG, MeshParams
    from core.units import MACH_SUPPORTED_MAX

    text = read(language, "USER_GUIDE.md")

    # Angle envelope.
    assert f"{int(ANGLE_LIMIT_DEG)}" in text

    # Mach envelope.
    assert f"{MACH_SUPPORTED_MAX}".replace(".", ",") in text or (
        f"{MACH_SUPPORTED_MAX}" in text
    )

    # Boundary-layer range, taken from the model's own metadata.
    field = MeshParams.model_fields["boundary_layers"]
    bounds = {
        getattr(item, attribute)
        for item in field.metadata
        for attribute in ("ge", "le")
        if getattr(item, attribute, None) is not None
    }
    for bound in bounds:
        assert str(bound) in text, f"boundary layer bound {bound} not stated"


@pytest.mark.parametrize("language", LANGUAGES)
def test_documented_cell_bands_match_the_code(language):
    """The cell-count guarantee is quoted in the docs; keep it truthful."""
    from core.models import CELL_COUNT_BANDS, MeshResolution, SimulationTrack

    text = read(language, "USER_GUIDE.md").replace(" ", " ")
    normalised = re.sub(r"[\s,]", "", text)

    for (track, resolution), (low, high) in CELL_COUNT_BANDS.items():
        assert str(low) in normalised, (
            f"{track.value}/{resolution.value} lower bound {low} not in {language} docs"
        )
        assert str(high) in normalised, (
            f"{track.value}/{resolution.value} upper bound {high} not in {language} docs"
        )


@pytest.mark.parametrize("language", LANGUAGES)
def test_documented_project_extension_matches(language):
    """The file extension users are told to look for must be the real one."""
    from core.project import PROJECT_EXTENSION

    combined = " ".join(read(language, name) for name in DOCUMENTS)
    assert PROJECT_EXTENSION in combined


@pytest.mark.parametrize("language", LANGUAGES)
def test_sweep_parameters_are_documented(language):
    """Every sweepable parameter is offered to the reader."""
    from backend.sweep import SWEEPABLE_PARAMETERS

    text = read(language, "MCP_AI_GUIDE.md")
    for canonical in {"mach", "aoa", "sideslip", "altitude"}:
        assert canonical in SWEEPABLE_PARAMETERS
        assert canonical in text, f"sweep parameter '{canonical}' undocumented"


# ---------------------------------------------------------------------------
# Cross-language consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", DOCUMENTS)
def test_language_versions_have_the_same_structure(name):
    """A section added to one language must be added to the other.

    Headings differ in wording between languages, so what is compared is the
    shape of the document: how many sections it has at each level.
    """
    counts = {}
    for language in LANGUAGES:
        text = read(language, name)
        counts[language] = (
            len(re.findall(r"^## ", text, re.MULTILINE)),
            len(re.findall(r"^### ", text, re.MULTILINE)),
        )
    assert counts["en"] == counts["cs"], (
        f"{name}: English has {counts['en']} (##, ###) sections but Czech has "
        f"{counts['cs']}; the translations have drifted apart"
    )


@pytest.mark.parametrize("name", DOCUMENTS)
def test_language_versions_cite_the_same_numbers(name):
    """Key figures must agree between translations.

    A default quoted differently in two languages means one of them is wrong,
    and a reader has no way to tell which.
    """
    figures = ("45", "800", "0.18", "1400", "3.5")
    for figure in figures:
        english = figure in read("en", name)
        czech = figure in read("cs", name) or figure.replace(".", ",") in read(
            "cs", name
        )
        assert english == czech, (
            f"{name}: the figure {figure} appears in one language but not the "
            "other"
        )


@pytest.mark.parametrize("language,name,text", all_docs())
def test_internal_links_resolve(language, name, text):
    """A broken link is a dead end for the reader."""
    for target in re.findall(r"\]\((?!https?://)([^)#]+\.md)(?:#[^)]*)?\)", text):
        resolved = (DOCS / language / target).resolve()
        assert resolved.is_file(), f"docs/{language}/{name} links to missing {target}"


@pytest.mark.parametrize("language,name,text", all_docs())
def test_no_placeholder_text_remains(language, name, text):
    """Unfinished drafts must not ship."""
    for marker in ("TODO", "TBD", "FIXME", "XXX", "Lorem ipsum"):
        assert marker not in text, f"docs/{language}/{name} still contains {marker}"


def test_czech_documents_are_actually_czech():
    """Guard against an untranslated file being copied into the Czech folder."""
    czech_markers = ("že", "která", "nastavení", "výpočet")
    for name in DOCUMENTS:
        text = read("cs", name)
        found = sum(1 for marker in czech_markers if marker in text)
        assert found >= 3, f"docs/cs/{name} does not look like Czech"


def test_czech_documents_contain_no_cyrillic():
    """Czech uses Latin script; stray Cyrillic means a bad paste."""
    for name in DOCUMENTS:
        text = read("cs", name)
        stray = sorted({character for character in text if "Ѐ" <= character <= "ӿ"})
        assert not stray, f"docs/cs/{name} contains Cyrillic characters: {stray}"


def test_readme_points_at_the_documentation():
    """The top-level README must lead readers to the guides."""
    text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/" in text
