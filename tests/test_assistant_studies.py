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


# ---------------------------------------------------------------------------
# evaluate, add_points, render
# ---------------------------------------------------------------------------


def test_evaluate_gives_the_model_at_single_conditions(isolated_data_root):
    """The roof comparison needed one condition, not a narrowed study."""
    import mcp_server
    from backend.shield_study import ShieldAnalyticModel

    reply = mcp_server.radiation_shield_study(
        action="evaluate",
        setup={"bottom_mode": "roof_temperature", "bottom_temperature_k": 343},
        conditions=[{"wind_speed_ms": 2.75}, {"wind_speed_ms": 0.5, "solar_flux_w_m2": 1200}],
    )
    assert reply["ok"], reply
    first, second = reply["rows"]
    assert first["roof_temperature_k"] == pytest.approx(343.0)
    setup = ShieldSetup(bottom_mode="roof_temperature", bottom_temperature_k=343)
    assert second["delta_t_k"] == pytest.approx(
        ShieldAnalyticModel(setup).delta_t(0.5, 1200, setup.baseline_bottom_flux())
    )
    one = mcp_server.sps30_housing_study(action="evaluate", conditions={"droplet_um": 20})
    assert one["ok"] and one["rows"][0]["droplet_um"] == 20


def test_unused_distribution_details_are_dropped(isolated_data_root, monkeypatch):
    import mcp_server

    monkeypatch.setattr(mcp_server, "_geometry_image", lambda kind, setup: {})
    reply = mcp_server.radiation_shield_study(
        action="analytic", study={"monte_carlo_samples": 1000},
        variables={"wind_speed_ms": {"minimum": 0.5, "maximum": 5, "std": 0, "mean": 0, "mode": 0}},
    )
    assert reply["ok"], reply


def _fake_solved_study(store, tmp_path):
    """A shield CFD study record with one solved point and a solution file."""
    import shutil

    import numpy as np
    import pyvista as pv

    from backend import shield_workflow
    from backend.shield_study import write_design_points_csv
    from core.shield_models import DesignPoint, ShieldCfdSettings

    params = ShieldStudyParams()
    record = store.create("shield", {})
    (record.directory / "params.json").write_text(params.model_dump_json())
    folder = record.path(shield_workflow.CFD_FOLDER)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "study.json").write_text(json.dumps({
        "setup": params.setup.model_dump(mode="json"),
        "cfd": ShieldCfdSettings().model_dump(mode="json"),
    }))
    (folder / "geometry.json").write_text(json.dumps({
        "shield_centre_m": [0.0, 0.0, 0.72], "shield_size_m": [0.2, 0.2, 0.2],
    }))
    points = [DesignPoint(name="DP0", wind_speed_ms=1.0, solar_flux_w_m2=1000, bottom_flux_w_m2=300)]
    write_design_points_csv(folder / "design_points.csv", params.setup, points)
    case = folder / "points" / "DP0"
    case.mkdir(parents=True)
    box = pv.ImageData(dimensions=(30, 30, 30), spacing=(0.05, 0.05, 0.05), origin=(-0.7, -0.7, 0.0))
    grid = box.cast_to_unstructured_grid()
    inside = np.all(np.abs(grid.points - [0, 0, 0.72]) < 0.08, axis=1)
    grid = grid.extract_points(~inside, adjacent_cells=False)
    grid.point_data["Temperature"] = 298.15 + np.exp(-np.linalg.norm(grid.points - [0, 0, 0.72], axis=1))
    grid.point_data["Velocity"] = np.tile([1.0, 0.0, 0.0], (grid.n_points, 1))
    grid.save(str(case / "flow.vtu"))
    (case / "result.json").write_text(json.dumps({"name": "DP0", "delta_t_k": 0.3, "converged": True}))
    return record.record_id


def test_render_draws_a_solved_point_into_the_graphics_folder(store, tmp_path, monkeypatch):
    pytest.importorskip("pyvista")
    import mcp_server

    monkeypatch.setattr(mcp_server, "_store", lambda: store)
    monkeypatch.setattr(mcp_server, "_geometry_image", lambda kind, setup: {})
    study = _fake_solved_study(store, tmp_path)
    reply = mcp_server.radiation_shield_study(
        action="render", study_id=study, point="DP0", render_quantity="all",
    )
    assert reply["ok"], reply
    assert len(reply["image_paths"]) == 4
    for path in reply["image_paths"]:
        assert Path(path).is_file() and "renders" in path
    from gui.gallery import find_rendered_images

    assert {Path(p) for p in reply["image_paths"]} <= {i.path for i in find_rendered_images(store)}


def test_extra_points_are_appended_for_the_next_solve(store, tmp_path, monkeypatch):
    import mcp_server
    from backend import shield_workflow

    monkeypatch.setattr(mcp_server, "_store", lambda: store)
    monkeypatch.setattr(mcp_server, "_geometry_image", lambda kind, setup: {})
    study = _fake_solved_study(store, tmp_path)
    reply = mcp_server.radiation_shield_study(
        action="add_points", study_id=study,
        conditions=[{"wind_speed_ms": 0.5, "solar_flux_w_m2": 1200}],
    )
    assert reply["ok"], reply
    assert reply["added"][0]["name"] == "X1"
    names = [p.name for p in shield_workflow.cfd_points(store, study)]
    assert names == ["DP0", "X1"]
    # DP0 keeps its result: it is read from its result.json, not the table.
    assert shield_workflow.cfd_points(store, study)[0].delta_t_k == pytest.approx(0.3)
    no_result = mcp_server.radiation_shield_study(action="add_points", study_id=study, worst_case=True)
    assert no_result["ok"] is False and "solve its design points" in no_result["error"]


def test_the_surface_is_checked_against_an_extra_cfd_point():
    from backend.shield_study import ShieldAnalyticModel, run_analytic_study
    from backend.shield_workflow import surface_check
    from core.shield_models import DesignPoint

    result = run_analytic_study(ShieldStudyParams(monte_carlo_samples=1000))
    worst = result.monte_carlo.worst_case
    model = ShieldAnalyticModel(result.params.setup)
    exact = model.delta_t(worst["wind_speed_ms"], worst["solar_flux_w_m2"], worst["bottom_flux_w_m2"])
    point = DesignPoint(
        name="X1", wind_speed_ms=worst["wind_speed_ms"], solar_flux_w_m2=worst["solar_flux_w_m2"],
        bottom_flux_w_m2=worst["bottom_flux_w_m2"], delta_t_k=exact,
    )
    row = surface_check(result, [point])[0]
    assert row["name"] == "X1"
    assert row["surface_delta_t_k"] == pytest.approx(worst["delta_t_k"], abs=1e-6)
    assert row["difference_k"] == pytest.approx(exact - worst["delta_t_k"])


# ---------------------------------------------------------------------------
# Optics by the thermometer, the open STEP, overview pictures
# ---------------------------------------------------------------------------


def test_faces_towards_the_thermometer_take_the_inside_optics():
    """Two plates with the thermometer between: both facing faces are 'inside'."""
    import numpy as np

    from backend.shield_cfd import RadiationFacets, facets_toward

    def square(z, up):
        a, b, c, d = [(-1, -1, z), (1, -1, z), (1, 1, z), (-1, 1, z)]
        return [(a, b, c), (a, c, d)] if up else [(a, c, b), (a, d, c)]

    triangles = np.array(square(1.0, True) + square(0.9, False) + square(0.1, True)
                         + square(0.0, False), dtype=float)
    normals = np.repeat([[0, 0, 1.0], [0, 0, -1.0], [0, 0, 1.0], [0, 0, -1.0]], 2, axis=0)
    toward = facets_toward(triangles, normals, [0.0, 0.0, 0.5])
    assert toward.tolist() == [False, False, True, True, True, True, False, False]
    facets = RadiationFacets(np.ones(8), normals, np.zeros(8), np.zeros(8), np.zeros(8), toward)
    setup = ShieldSetup(
        optics_orientation="thermometer",
        top_side_solar_absorptivity=0.15, top_side_emissivity=0.1,
        bottom_side_solar_absorptivity=0.95, bottom_side_emissivity=0.9,
    )
    alpha, emissivity = facets.optics(setup)
    assert alpha.tolist() == pytest.approx([0.15, 0.15, 0.95, 0.95, 0.95, 0.95, 0.15, 0.15])
    assert emissivity[2] == pytest.approx(0.9) and emissivity[0] == pytest.approx(0.1)
    vertical = facets.optics(setup.model_copy(update={"optics_orientation": "vertical"}))[0]
    assert vertical[2] == pytest.approx(0.95) and vertical[4] == pytest.approx(0.15)


def test_the_analytic_model_uses_the_outside_optics_by_the_thermometer():
    import math

    from backend.shield_study import ShieldAnalyticModel

    shiny = ShieldSetup(shield_solar_absorptivity=0.15, shield_emissivity=0.1)
    inside_black = shiny.model_copy(update={
        "optics_orientation": "thermometer",
        "bottom_side_solar_absorptivity": 0.95, "bottom_side_emissivity": 0.9,
    })
    value = ShieldAnalyticModel(inside_black).delta_t(1, 1000, 300)
    assert math.isfinite(value)
    assert value == pytest.approx(ShieldAnalyticModel(shiny).delta_t(1, 1000, 300))


def test_the_geometry_falls_back_to_the_step_open_in_the_study_tab(isolated_data_root, tmp_path):
    import mcp_server
    from core.workspace import set_active_study

    step = tmp_path / "Radiation Shield Small.step"
    step.write_text("ISO-10303-21;")
    tab = ShieldStudyParams.model_validate(
        {"setup": {"shield_step_path": str(step), "scale_to_meters": 0.001}}
    )
    set_active_study("shield", tab.model_dump(mode="json"))
    reply = mcp_server.get_active_geometry()
    assert reply["ok"] and reply["geometry"]["step_file_path"] == str(step.resolve())
    assert reply["source"] == "Radiation Shield tab"
    bare = mcp_server.get_active_geometry(step_file_path="Radiation Shield Small.step")
    assert bare["ok"] and Path(bare["geometry"]["step_file_path"]) == step.resolve()
    assert "note" in bare
    missing = mcp_server.get_active_geometry(step_file_path="nothing.step")
    assert missing["ok"] is False and missing["files_open_in_study_tabs"]


def test_overview_and_worst_point_pictures(store, tmp_path, monkeypatch):
    pytest.importorskip("pyvista")
    import mcp_server

    monkeypatch.setattr(mcp_server, "_store", lambda: store)
    monkeypatch.setattr(mcp_server, "_geometry_image", lambda kind, setup: {})
    study = _fake_solved_study(store, tmp_path)
    reply = mcp_server.radiation_shield_study(action="render", study_id=study, point="worst")
    assert reply["ok"], reply
    assert reply["point"] == "DP0" and reply["image_path"].endswith("DP0_overview_y.png")
    assert Path(reply["image_path"]).is_file()
    from backend import shield_workflow

    pictures = mcp_server._auto_renders("shield", store, shield_workflow, study)
    assert pictures["rendered_points"] == ["DP0"] and Path(pictures["image_path"]).is_file()
