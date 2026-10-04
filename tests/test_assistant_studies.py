"""The assistant and the sensor studies: typed parameters, the open tab,
two-sided plates, STEP size, repeated calls, and clickable pictures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.shield_models import ShieldSetup, ShieldStudyParams


# ---------------------------------------------------------------------------
# Tool schemas a model can actually fill
# ---------------------------------------------------------------------------


def _schema(name: str) -> dict:
    from backend.ai_agent import build_tool_schemas

    for schema in build_tool_schemas():
        if schema["function"]["name"] == name:
            return schema["function"]["parameters"]
    raise AssertionError(name)


@pytest.mark.parametrize(
    "tool, field",
    [("radiation_shield_study", "shield_emissivity"), ("sps30_housing_study", "fan_flow_lpm")],
)
def test_the_setup_is_an_object_with_its_fields_listed(tool, field):
    """A bare object was stripped to {} by some providers: list the fields."""
    parameters = _schema(tool)
    setup = parameters["properties"]["setup"]
    assert setup["type"] == "object"
    assert field in setup["properties"]
    text = json.dumps(parameters)
    assert "$ref" not in text and "anyOf" not in text
    wind = parameters["properties"]["variables"]["properties"]
    assert "minimum" in next(iter(wind.values()))["properties"]


def test_overrides_arrive_as_objects_dicts_or_json_text(isolated_data_root, monkeypatch):
    import mcp_server

    monkeypatch.setattr(mcp_server, "_geometry_image", lambda kind, setup: {})
    reply = mcp_server.radiation_shield_study(
        action="analytic",
        setup={"shield_solar_absorptivity": 0.15, "shield_emissivity": 0.5},
        study='{"monte_carlo_samples": 1000}',
    )
    assert reply["ok"], reply
    assert reply["applied_optics"]["top"] == {"solar_absorptivity": 0.15, "emissivity": 0.5}
    assert reply["applied_study"]["monte_carlo_samples"] == 1000
    bad = mcp_server.radiation_shield_study(action="analytic", setup={"shield_emisivity": 0.5})
    assert bad["ok"] is False and "shield_emisivity" in bad["error"]


def test_the_shield_open_on_the_tab_is_the_base(isolated_data_root, tmp_path, monkeypatch):
    """No study_id and no path: the operator's open shield, not the built-in one."""
    import mcp_server
    from core.workspace import set_active_study

    monkeypatch.setattr(mcp_server, "_geometry_image", lambda kind, setup: {})
    step = tmp_path / "mine.step"
    step.write_text("ISO-10303-21;")
    tab = ShieldStudyParams.model_validate(
        {"setup": {"shield_step_path": str(step), "shield_emissivity": 0.4}}
    )
    set_active_study("shield", tab.model_dump(mode="json"))
    reply = mcp_server.radiation_shield_study(
        action="analytic", setup={"shield_solar_absorptivity": 0.15},
        study={"monte_carlo_samples": 1000},
    )
    assert reply["ok"], reply
    assert reply["setup_source"] == "the Radiation shield study tab"
    assert reply["applied_setup"]["shield_step_path"] == str(step)
    assert reply["applied_setup"]["shield_emissivity"] == 0.4
    assert reply["applied_setup"]["shield_solar_absorptivity"] == 0.15


# ---------------------------------------------------------------------------
# Two-sided plates
# ---------------------------------------------------------------------------


def test_equal_sides_change_nothing():
    from backend.shield_study import ShieldAnalyticModel

    plain = ShieldSetup(shield_solar_absorptivity=0.3, shield_emissivity=0.6)
    explicit = plain.model_copy(
        update={"top_side_solar_absorptivity": 0.3, "top_side_emissivity": 0.6,
                "bottom_side_solar_absorptivity": 0.3, "bottom_side_emissivity": 0.6}
    )
    assert not explicit.two_sided()
    assert ShieldAnalyticModel(plain).delta_t(1, 1000, 300) == pytest.approx(
        ShieldAnalyticModel(explicit).delta_t(1, 1000, 300)
    )


def test_a_black_underside_warms_the_shield_over_a_warm_ground():
    """A black underside absorbs the ground's long-wave a shiny one reflects."""
    from backend.shield_study import ShieldAnalyticModel

    shiny = ShieldSetup(shield_solar_absorptivity=0.15, shield_emissivity=0.1)
    black_under = shiny.model_copy(
        update={"bottom_side_solar_absorptivity": 0.95, "bottom_side_emissivity": 0.9}
    )
    assert black_under.two_sided()
    hot_ground = 750.0
    assert ShieldAnalyticModel(black_under).delta_t(1, 1000, hot_ground) > ShieldAnalyticModel(
        shiny
    ).delta_t(1, 1000, hot_ground)


def test_facets_take_the_optics_of_the_way_they_face():
    import numpy as np

    from backend.shield_cfd import RadiationFacets

    facets = RadiationFacets.__new__(RadiationFacets)
    facets.normals = np.array([[0, 0, 1.0], [0, 0, -1.0], [1.0, 0, 0]])
    setup = ShieldSetup(
        top_side_solar_absorptivity=0.15, top_side_emissivity=0.1,
        bottom_side_solar_absorptivity=0.95, bottom_side_emissivity=0.9,
    )
    alpha, emissivity = facets.optics(setup)
    assert alpha.tolist() == pytest.approx([0.15, 0.95, 0.55])
    assert emissivity.tolist() == pytest.approx([0.1, 0.9, 0.5])


def test_the_fluent_package_explains_two_sided_walls():
    from backend.shield_export import _two_sided_readme

    setup = ShieldSetup(top_side_emissivity=0.1, bottom_side_emissivity=0.9)
    text = _two_sided_readme(setup)
    assert "sep-face-zone-angle" in text and "0.9" in text
    assert _two_sided_readme(ShieldSetup()) == ""


# ---------------------------------------------------------------------------
# STEP size, repeated calls
# ---------------------------------------------------------------------------


def test_a_part_drawn_away_from_the_origin_measures_its_own_size(tmp_path):
    """Axis-placement origins at (0,0,0) must not stretch the extent."""
    import gmsh

    from backend.gmsh_session import gmsh_session
    from core.step_inspect import largest_extent

    path = tmp_path / "far.step"
    with gmsh_session("far"):
        gmsh.model.occ.addBox(160, 0, -30, 55, 60, 2)
        gmsh.model.occ.synchronize()
        gmsh.write(str(path))
    assert largest_extent(path) == pytest.approx(60.0, rel=1e-3)


class _ScriptedClient:
    """Asks for the same call over and over, then answers."""

    def __init__(self, calls: int) -> None:
        self.calls = calls

    def complete(self, messages, tools=None, model=None):
        if self.calls:
            self.calls -= 1
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"c{self.calls}", "type": "function",
                    "function": {"name": "radiation_shield_study",
                                 "arguments": json.dumps({"action": "sweep",
                                                          "sweep_variable": "wind_speed_ms",
                                                          "sweep_start": 1, "sweep_stop": 2,
                                                          "sweep_step": 1})},
                }],
            }
        return {"role": "assistant", "content": "done"}


def test_an_identical_call_is_not_run_twice(isolated_data_root, monkeypatch):
    import mcp_server
    from backend.ai_agent import AIAssistant

    monkeypatch.setattr(mcp_server, "_geometry_image", lambda kind, setup: {})
    assistant = AIAssistant(client=_ScriptedClient(3), model="test")
    reply = assistant.ask("sweep it")
    assert [call.succeeded for call in reply.tool_calls] == [True, False, False]
    assert "already made" in reply.tool_calls[1].error


# ---------------------------------------------------------------------------
# Pictures in the transcript
# ---------------------------------------------------------------------------


def _png(path: Path) -> Path:
    from PySide6 import QtGui

    image = QtGui.QImage(640, 320, QtGui.QImage.Format.Format_RGB32)
    image.fill(0x336699)
    image.save(str(path))
    return path


def _anchor_point(tab, text: str):
    """Viewport point of the first fragment whose text starts with ``text``."""
    from PySide6 import QtCore, QtGui

    block = tab.transcript.document().begin()
    while block.isValid():
        iterator = block.begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            if fragment.text().startswith(text):
                cursor = QtGui.QTextCursor(tab.transcript.document())
                cursor.setPosition(fragment.position())
                return tab.transcript.cursorRect(cursor).center() + QtCore.QPoint(4, 0)
            iterator += 1
        block = block.next()
    raise AssertionError(text)


def test_real_clicks_open_the_viewer_and_the_graphics_tab(isolated_data_root, tmp_path):
    """Mouse clicks, with a path that has spaces in it, like the operator's."""
    from PySide6 import QtCore, QtTest

    from core.settings import AppSettings
    from gui.ai_panel import AITab
    from gui.main_window import build_application

    app = build_application([])
    folder = tmp_path / "My Pictures"
    folder.mkdir()
    image = _png(folder / "shield geometry.png")
    tab = AITab(AppSettings(), isolated_data_root)
    tab.resize(900, 900)
    tab.show()
    tab._append_image(image)
    app.processEvents()
    wanted = []
    tab.showGraphics.connect(wanted.append)

    for label in ("￼", "Enlarge"):
        QtTest.QTest.mouseClick(
            tab.transcript.viewport(), QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier, _anchor_point(tab, label),
        )
        app.processEvents()
        viewer = tab._viewers[-1]
        assert viewer.isVisible() and viewer.path == image
        viewer.close()
    # A closed viewer must not break the next click.
    tab.open_image(image).close()

    QtTest.QTest.mouseClick(
        tab.transcript.viewport(), QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.KeyboardModifier.NoModifier, _anchor_point(tab, "Open in the Graphics"),
    )
    app.processEvents()
    assert wanted == [str(image)]

    # A conversation restored from history keeps working links.
    html = tab.transcript.toHtml()
    tab._transcript_images = []
    tab.transcript.setHtml(html)
    tab._rebuild_image_index()
    assert tab._image_for("ats-image:0") == image
    tab.deleteLater()


def test_the_graphics_tab_shows_previews_and_any_picture(store, tmp_path):
    from gui.gallery import find_rendered_images

    previews = store.runs_dir.parent / "previews"
    previews.mkdir(parents=True, exist_ok=True)
    preview = _png(previews / "shield-geometry-abc.png")
    elsewhere = _png(tmp_path / "chart.png")
    found = {image.path for image in find_rendered_images(store, (elsewhere,))}
    assert preview in found and elsewhere in found
