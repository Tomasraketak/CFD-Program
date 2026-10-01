"""One-input sweeps and geometry pictures for the sensor studies."""

from __future__ import annotations

import csv

import pytest

from backend import parameter_sweep as sweeps
from backend.shield_study import ShieldAnalyticModel
from backend.sps30_study import Sps30AnalyticModel
from core.shield_models import ShieldSetup
from core.sps30_models import Sps30Setup


def test_steps_include_both_ends_without_rounding_drift():
    """0.2 to 5 every 0.2 is 25 values ending exactly at 5, not 4.999999."""
    values = sweeps.sweep_values(0.2, 5.0, 0.2)
    assert len(values) == 25
    assert values[0] == 0.2 and values[-1] == 5.0
    assert values[13] == 2.8


def test_a_stop_off_the_grid_is_still_reached():
    assert sweeps.sweep_values(0.0, 1.0, 0.3) == [0.0, 0.3, 0.6, 0.9, 1.0]


@pytest.mark.parametrize(
    "start, stop, step",
    [(1.0, 2.0, 0.0), (1.0, 2.0, -0.1), (2.0, 1.0, 0.1), (0.0, 1e6, 1e-3)],
)
def test_impossible_steps_are_refused(start, stop, step):
    with pytest.raises(sweeps.SweepError):
        sweeps.sweep_values(start, stop, step)


def test_every_shield_point_is_the_model_itself():
    """No surface in between: each row is what the model gives at that wind."""
    setup = ShieldSetup(bottom_flux_w_m2=550.0)
    result = sweeps.shield_sweep(setup, "wind_speed_ms", 0.2, 5.0, 0.2)
    model = ShieldAnalyticModel(setup)
    for row in result.rows:
        assert row["solar_flux_w_m2"] == 1000.0 and row["bottom_flux_w_m2"] == 550.0
        assert row["delta_t_k"] == pytest.approx(
            model.delta_t(row["wind_speed_ms"], 1000.0, 550.0)
        )
    # More wind, less error: the physical curve has no invented minimum.
    errors = result.column("delta_t_k")
    assert all(a > b for a, b in zip(errors, errors[1:]))
    assert all(e > 0.0 for e in errors)
    assert any("0.5 m/s" in note for note in result.notes)


def test_every_sps30_point_is_the_model_itself():
    setup = Sps30Setup(speed_ms=25.0, yaw_deg=10.0)
    result = sweeps.sps30_sweep(setup, "droplet_um", 10.0, 100.0, 10.0)
    model = Sps30AnalyticModel(setup)
    assert len(result.rows) == 10
    for row in result.rows:
        expected = model.solve(25.0, 10.0, row["droplet_um"])
        assert row["penetration"] == pytest.approx(expected["penetration"])
        assert row["face_velocity_ms"] == pytest.approx(expected["face_velocity_ms"])


def test_unknown_inputs_and_unphysical_values_are_refused():
    with pytest.raises(sweeps.SweepError, match="unknown variable"):
        sweeps.shield_sweep(ShieldSetup(), "humidity", 0.0, 1.0, 0.1)
    with pytest.raises(sweeps.SweepError, match="above 0"):
        sweeps.shield_sweep(ShieldSetup(), "wind_speed_ms", 0.0, 1.0, 0.1)
    with pytest.raises(sweeps.SweepError, match="above 0"):
        sweeps.sps30_sweep(Sps30Setup(), "speed_ms", 0.0, 10.0, 1.0)


def test_a_saved_sweep_has_its_csv_and_chart(tmp_path):
    result = sweeps.shield_sweep(ShieldSetup(), "solar_flux_w_m2", 0.0, 1000.0, 250.0)
    sweeps.save_sweep(result, tmp_path)
    with open(result.csv_path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [float(r["solar_flux_w_m2"]) for r in rows] == [0, 250, 500, 750, 1000]
    assert result.image_path and (tmp_path / result.image_path).stat().st_size > 1000
    reply = sweeps.as_dict(result)
    assert reply["points"] == 5 and reply["held_fixed"]["wind_speed_ms"] == 1.0


def test_the_tools_sweep_at_the_steps_asked_for(isolated_data_root, monkeypatch):
    """The request that failed before: wind 0.2-5 m/s every 0.2 m/s."""
    import mcp_server

    monkeypatch.setattr(mcp_server, "_geometry_image", lambda kind, setup: {})
    reply = mcp_server.radiation_shield_study(
        action="sweep", setup={"bottom_flux_w_m2": 550},
        sweep_variable="wind_speed_ms", sweep_start=0.2, sweep_stop=5, sweep_step=0.2,
    )
    assert reply["ok"], reply
    assert reply["points"] == 25
    assert reply["rows"][0]["wind_speed_ms"] == 0.2
    assert reply["rows"][0]["delta_t_k"] == pytest.approx(
        ShieldAnalyticModel(ShieldSetup(bottom_flux_w_m2=550)).delta_t(0.2, 1000, 550), abs=1e-5
    )
    missing = mcp_server.radiation_shield_study(action="sweep", sweep_variable="wind_speed_ms")
    assert missing["ok"] is False and "sweep_step" in missing["error"]
    sps = mcp_server.sps30_housing_study(
        action="sweep", sweep_variable="yaw_deg", sweep_start=-20, sweep_stop=20, sweep_step=10,
    )
    assert sps["ok"] and [r["yaw_deg"] for r in sps["rows"]] == [-20, -10, 0, 10, 20]
    assert not mcp_server.is_long_running("radiation_shield_study", {"action": "sweep"})


def test_the_assistant_sees_the_sweep_parameters_and_is_told_to_use_them():
    from backend.ai_agent import SYSTEM_PROMPT, build_tool_schemas

    for schema in build_tool_schemas():
        if schema["function"]["name"] in ("radiation_shield_study", "sps30_housing_study"):
            properties = schema["function"]["parameters"]["properties"]
            for name in ("sweep_variable", "sweep_start", "sweep_stop", "sweep_step"):
                assert name in properties
            assert "'sweep'" in schema["function"]["description"]
    assert "Never build such a table by reading" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Geometry pictures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind, setup", [("shield", ShieldSetup()), ("sps30", Sps30Setup())])
def test_the_geometry_is_drawn_once_per_setup(tmp_path, kind, setup):
    pytest.importorskip("pyvista")
    from backend.study_preview import cached_preview

    path = cached_preview(kind, setup, tmp_path)
    assert path.is_file() and path.stat().st_size > 20_000
    stamp = path.stat().st_mtime_ns
    assert cached_preview(kind, setup, tmp_path) == path
    assert path.stat().st_mtime_ns == stamp
    changed = setup.model_copy(update={"fan_enabled": False} if kind == "sps30" else {"plate_count": 4})
    assert cached_preview(kind, changed, tmp_path) != path


def test_the_preview_action_returns_a_picture(isolated_data_root):
    pytest.importorskip("pyvista")
    import mcp_server

    reply = mcp_server.sps30_housing_study(action="preview")
    assert reply["ok"], reply
    assert reply["image_path"].endswith(".png") and "baffle" in reply["shows"]


def test_the_assistant_panel_shows_chart_and_geometry(tmp_path):
    from gui.ai_panel import _image_paths

    chart = tmp_path / "a.png"
    geometry = tmp_path / "b.png"
    chart.write_bytes(b"x")
    geometry.write_bytes(b"x")
    reply = {"ok": True, "image_path": str(chart), "geometry_image_path": str(geometry)}
    assert _image_paths(reply) == [chart, geometry]
    assert _image_paths({"ok": False, "image_path": str(chart)}) == []


def test_the_panels_sweep_and_show_the_rows(store):
    from gui.main_window import build_application
    from gui.shield_panel import ShieldStudyPanel
    from gui.sps30_panel import Sps30StudyPanel

    build_application([])
    shield = ShieldStudyPanel(store)
    assert shield.sweep_controls.values() == ("wind_speed_ms", 0.2, 5.0, 0.2)
    shield._show_sweep(sweeps.shield_sweep(ShieldSetup(), *shield.sweep_controls.values()))
    assert shield.sweep_view.table.rowCount() == 25
    assert shield.result_tabs.currentWidget() is shield.sweep_view

    housing = Sps30StudyPanel(store)
    housing.sweep_controls.set_values("speed_ms", 5.0, 35.0, 5.0)
    housing._show_sweep(sweeps.sps30_sweep(Sps30Setup(), *housing.sweep_controls.values()))
    assert housing.sweep_view.table.rowCount() == 7


def test_a_transcript_picture_opens_in_a_zoomable_viewer(tmp_path):
    from PySide6 import QtCore

    from gui.ai_panel import IMAGE_SCHEME, ImageViewer
    from gui.main_window import build_application

    app = build_application([])
    picture = tmp_path / "p.png"
    from PySide6 import QtGui

    image = QtGui.QImage(800, 400, QtGui.QImage.Format.Format_RGB32)
    image.fill(0x336699)
    image.save(str(picture))
    viewer = ImageViewer(picture)
    viewer.show()
    app.processEvents()
    viewer.actual_size()
    assert viewer.label.pixmap().width() == 800
    viewer.set_zoom(2.0)
    assert viewer.label.pixmap().width() == 1600
    viewer.close()
    url = QtCore.QUrl(IMAGE_SCHEME + ":" + str(picture))
    assert url.scheme() == IMAGE_SCHEME and url.path() == str(picture)
