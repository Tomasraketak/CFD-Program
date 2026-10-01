"""SPS30 housing study: lumped model, design exploration, CFD pieces, tools."""

from __future__ import annotations

import numpy as np
import pytest

from backend import sps30_study as study
from backend.design_exploration import Axis, Surface, central_composite, encode
from core.sps30_models import (
    Sps30CfdSettings,
    Sps30Evaluator,
    Sps30Point,
    Sps30Setup,
    Sps30StudyParams,
    Sps30Variable,
)


# ---------------------------------------------------------------------------
# Generic design exploration
# ---------------------------------------------------------------------------


def test_a_three_input_ccd_has_fifteen_points():
    points = central_composite(3)
    assert points.shape == (15, 3)
    assert len({tuple(p) for p in points}) == 15


def test_a_log_axis_samples_every_decade_alike():
    axis = Axis("d", 10.0, 1000.0, log=True)
    values = axis.sample(40000, np.random.default_rng(1))
    assert values.min() >= 10.0 and values.max() <= 1000.0
    # Half the samples below the geometric middle, 100.
    assert np.mean(values < 100.0) == pytest.approx(0.5, abs=0.02)
    assert axis.decode(axis.encode(np.array([37.0])))[0] == pytest.approx(37.0)


def test_surfaces_reproduce_what_they_can():
    x = central_composite(3)
    y = 1.0 + x[:, 0] - 2.0 * x[:, 1] ** 2 + 0.5 * x[:, 0] * x[:, 2]
    quadratic = Surface(x, y, "quadratic")
    assert quadratic.r_squared() == pytest.approx(1.0)
    assert quadratic.leave_one_out() < 1e-9
    rbf = Surface(x, y, "rbf")
    assert rbf.predict(x) == pytest.approx(y, abs=1e-8)


# ---------------------------------------------------------------------------
# Lumped model
# ---------------------------------------------------------------------------


def test_the_defaults_are_the_specified_ranges():
    p = Sps30StudyParams()
    assert (p.speed_ms.minimum, p.speed_ms.maximum) == (5.0, 35.0)
    assert (p.yaw_deg.minimum, p.yaw_deg.maximum) == (-20.0, 20.0)
    assert (p.droplet_um.minimum, p.droplet_um.maximum) == (10.0, 2000.0)
    assert p.droplet_um.log
    assert p.setup.max_face_velocity_ms == 1.0 and p.setup.max_penetration == 0.0


def test_crosswind_drives_air_through_the_static_ports():
    """At zero yaw the two slits balance; yaw drives a cross-flow."""
    model = study.Sps30AnalyticModel(Sps30Setup())
    calm = model.solve(20.0, 0.0, 100.0)
    yawed = model.solve(20.0, 15.0, 100.0)
    mirrored = model.solve(20.0, -15.0, 100.0)
    assert yawed["port_flow_lpm"] > 3 * calm["port_flow_lpm"]
    assert yawed["face_velocity_ms"] == pytest.approx(mirrored["face_velocity_ms"])
    assert model.solve(35.0, 15.0, 100.0)["face_velocity_ms"] > yawed["face_velocity_ms"]


def test_heavy_rain_is_stopped_and_fine_mist_is_not():
    model = study.Sps30AnalyticModel(Sps30Setup())
    assert model.solve(20.0, 5.0, 2000.0)["penetration"] == pytest.approx(0.0, abs=1e-9)
    assert model.solve(20.0, 0.0, 10.0)["penetration"] > 1e-3


def test_without_the_fan_nothing_is_drawn_onto_the_face():
    model = study.Sps30AnalyticModel(Sps30Setup(fan_enabled=False))
    assert model.solve(20.0, 0.0, 10.0)["penetration"] == 0.0


def test_the_analytic_study_reports_every_goal():
    result = study.run_analytic_study(Sps30StudyParams(monte_carlo_samples=2000))
    assert result.evaluator is Sps30Evaluator.ANALYTIC
    assert len(result.points) == 15
    assert set(result.outputs) == {"face_velocity_ms", "penetration", "exchange_flow_lpm"}
    assert set(result.reliability_by_goal) == {"face_velocity", "penetration"}
    assert 0.0 <= result.reliability <= min(result.reliability_by_goal.values())
    # Fine mist gets through the analytic housing, and it says so.
    assert any("fine mist" in note for note in result.notes)
    worst = result.outputs["penetration"].worst_case
    assert worst["droplet_um"] < 50.0


def test_an_exchange_goal_is_checked_when_set():
    params = Sps30StudyParams.model_validate(
        {"setup": {"min_exchange_flow_lpm": 5.0}, "monte_carlo_samples": 1000}
    )
    result = study.run_analytic_study(params)
    assert "exchange_flow" in result.reliability_by_goal
    assert result.reliability_by_goal["exchange_flow"] < 1.0


def test_bad_ranges_are_refused():
    with pytest.raises(ValueError):
        Sps30Variable(minimum=5.0, maximum=1.0)
    with pytest.raises(ValueError):
        Sps30Variable(minimum=0.0, maximum=1.0, log=True)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def test_points_round_trip_through_csv(tmp_path):
    params = Sps30StudyParams()
    points = study.solve_points(study.design_points(params), study.analytic_solver(params.setup), "a")
    path = study.write_points_csv(tmp_path / "dp.csv", points)
    back = study.read_points_csv(path, params.setup)
    assert [p.name for p in back] == [p.name for p in points]
    assert [p.face_velocity_ms for p in back] == pytest.approx([p.face_velocity_ms for p in points], rel=1e-5)


def test_a_workbench_export_with_trap_counts_is_understood(tmp_path):
    path = tmp_path / "wb.csv"
    path.write_text(
        "# Design points\n"
        "Name,P1 - speed [m s^-1],P2 - yaw [degree],P3 - droplet_diameter [m],"
        "P4 - face_velocity [m s^-1],P5 - sensor_trap_count,P6 - injected,P7 - ux_integral [m^3 s^-1]\n"
        "1,20,0,0.0001,0.05,0,5000,2e-5\n"
        "2,35,20,0.00001,0.2,25,5000,1e-4\n",
        encoding="utf-8",
    )
    points = study.read_points_csv(path, Sps30Setup())
    assert points[0].droplet_um == pytest.approx(100.0)
    assert points[0].penetration == 0.0
    assert points[1].penetration == pytest.approx(25 / 5000)
    assert points[0].exchange_flow_lpm == pytest.approx(0.5 * 2e-5 * 60000)


def test_a_trap_count_without_injected_droplets_is_refused(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("speed,yaw,droplet_diameter,face_velocity,sensor_trap_count,exchange_flow_lpm\n20,0,100,0.1,3,1\n")
    with pytest.raises(study.Sps30StudyError, match="injected"):
        study.read_points_csv(path, Sps30Setup())


# ---------------------------------------------------------------------------
# CFD pieces
# ---------------------------------------------------------------------------


def test_the_su2_config_turns_the_wind_and_models_the_fan():
    from backend.sps30_cfd import build_sps30_config

    setup = Sps30Setup()
    point = Sps30Point(name="DP1", speed_ms=20.0, yaw_deg=10.0, droplet_um=50.0)
    text = build_sps30_config(setup, Sps30CfdSettings(), point, domain_normal=[-1.0, 0.0, 0.0])
    assert "KIND_TURB_MODEL= SST" in text
    assert "MARKER_INLET= ( INLET, 288.1500, 20.000000, 0.98480775, 0.17364818, 0.0, SIDE_NEG," in text
    assert "INC_OUTLET_TYPE= PRESSURE_OUTLET, PRESSURE_OUTLET" in text
    assert "MARKER_OUTLET= ( OUTLET, 0.0, SIDE_POS, 0.0 )" in text
    # The fan: 0.3 L/min sucked through the 20 x 20 mm face, into the sensor.
    assert "SENSOR, 288.1500, 0.01250000, 1.00000000, -0.00000000, -0.00000000" in text
    assert "MARKER_SYM= ( TOP, BOTTOM )" in text
    calm = build_sps30_config(Sps30Setup(fan_enabled=False), Sps30CfdSettings(), point.model_copy(update={"yaw_deg": 0.0}))
    assert "MARKER_SYM= ( TOP, BOTTOM, SIDE_NEG, SIDE_POS )" in calm
    assert "MARKER_HEATFLUX= ( HOUSING, 0.0, SENSOR, 0.0 )" in calm
    left = build_sps30_config(
        setup, Sps30CfdSettings(), point.model_copy(update={"yaw_deg": -10.0}), domain_normal=[-1, 0, 0]
    )
    assert "SIDE_POS, 288" in left and "SIDE_NEG, 0.0" in left


def test_the_travel_direction_turns_onto_minus_x():
    from backend.sps30_cfd import travel_rotation

    for axis, vector in (("-x", (-1, 0, 0)), ("+x", (1, 0, 0)), ("+y", (0, 1, 0)), ("-y", (0, -1, 0))):
        assert travel_rotation(axis) @ np.array(vector, float) == pytest.approx([-1.0, 0.0, 0.0], abs=1e-12)


def test_droplets_carried_onto_the_sensor_are_counted():
    """In a uniform stream every droplet through the housing box hits the face."""
    import pyvista as pv

    from backend.sps30_cfd import Sps30Domain, track_droplets

    grid = pv.ImageData(dimensions=(34, 21, 21), spacing=(0.02, 0.02, 0.02), origin=(0.0, -0.2, -0.2))
    mesh = grid.cast_to_unstructured_grid()
    mesh.point_data["Velocity"] = np.tile([5.0, 0.0, 0.0], (mesh.n_points, 1))
    domain = Sps30Domain(
        housing_low=[0.4, -0.05, -0.05], housing_high=[0.6, 0.05, 0.05],
        sensor_center=[0.66, 0.0, 0.0], sensor_normal=[-1.0, 0.0, 0.0], sensor_size=[0.1, 0.1],
        chamber_plane_x=0.5, reference_length=0.2, cell_count=mesh.n_cells, marker_counts={},
    )
    # The sensor: a finely triangulated square on the tunnel's end, as a
    # real mesh's wall facets are; the "housing" a facet far upstream.
    ys = np.linspace(-0.1, 0.1, 21)
    square = np.array([[0.66, y, z] for y in ys for z in ys])
    tris = []
    for i in range(20):
        for j in range(20):
            a = i * 21 + j
            tris += [[a, a + 1, a + 21], [a + 1, a + 22, a + 21]]
    pts = np.vstack([square, [[0.0, -0.2, -0.2], [0.0, 0.2, 0.0], [0.0, 0.0, 0.2]]])
    walls = {"points": pts, "sensor": np.array(tris), "housing": np.array([[441, 442, 443]])}
    point = Sps30Point(name="t", speed_ms=5.0, yaw_deg=0.0, droplet_um=50.0)
    result = track_droplets(mesh, domain, Sps30Setup(), point, 60, random_walk=False, walls=walls)
    assert result.entered > 30
    assert result.sensor_hits == result.entered
    assert result.penetration == pytest.approx(1.0)


def test_the_built_in_housing_meshes_with_a_sensor_face(tmp_path, monkeypatch):
    from backend import sps30_cfd

    monkeypatch.setitem(sps30_cfd.RESOLUTION_SIZES, "coarse", (0.004, 0.1))
    domain = sps30_cfd.mesh_sps30_domain(Sps30Setup(), tmp_path, "coarse")
    assert domain.marker_counts["SENSOR"] > 0
    assert domain.housing_high[0] - domain.housing_low[0] == pytest.approx(0.12, abs=1e-3)
    text = (tmp_path / sps30_cfd.MESH_FILENAME).read_text()
    for name in sps30_cfd.MARKERS:
        assert f"MARKER_TAG= {name}\n" in text


def test_a_wrong_sensor_face_is_explained(tmp_path, monkeypatch):
    from backend import sps30_cfd

    monkeypatch.setitem(sps30_cfd.RESOLUTION_SIZES, "coarse", (0.004, 0.1))
    with pytest.raises(sps30_cfd.Sps30CfdError, match="sensor face"):
        sps30_cfd.mesh_sps30_domain(Sps30Setup(sensor_face_center_m=[0.05, 0.0, 0.03]), tmp_path, "coarse")


def test_the_fluent_package_is_complete(tmp_path):
    from backend.sps30_export import export_fluent_package

    folder = export_fluent_package(Sps30StudyParams(), tmp_path / "fluent")
    for name in ("fluid_domain.step", "housing_placed.step", "design_points.csv", "README_FLUENT.md", "study.json"):
        assert (folder / name).is_file(), name
    readme = (folder / "README_FLUENT.md").read_text()
    assert "k-omega SST" in readme and "Discrete Random Walk" in readme and "trap" in readme


# ---------------------------------------------------------------------------
# Stored studies, MCP and GUI
# ---------------------------------------------------------------------------


def test_the_tool_runs_imports_and_explains(store, tmp_path):
    import mcp_server

    reply = mcp_server.sps30_housing_study(action="analytic", study={"monte_carlo_samples": 1000})
    assert reply["ok"], reply
    assert reply["study_id"].startswith("sps30-")
    params = Sps30StudyParams()
    points = study.solve_points(study.design_points(params), study.analytic_solver(params.setup), "x")
    path = study.write_points_csv(tmp_path / "solved.csv", points)
    imported = mcp_server.sps30_housing_study(action="import", results_csv=str(path))
    assert imported["ok"] and imported["evaluator"] == "imported"
    status = mcp_server.sps30_housing_study(action="status", study_id=imported["study_id"])
    assert status["ok"] and "outputs" in status
    assert mcp_server.sps30_housing_study(action="fly")["ok"] is False
    bad = mcp_server.sps30_housing_study(action="analytic", variables={"yaw_deg": {"minimum": 5, "maximum": -5}})
    assert bad["ok"] is False
    assert mcp_server.is_long_running("sps30_housing_study", {"action": "solve_cfd"})
    assert not mcp_server.is_long_running("sps30_housing_study", {"action": "analytic"})


def test_the_gui_panel_round_trips_and_runs(store):
    from gui.main_window import build_application
    from gui.sps30_panel import Sps30StudyPanel

    build_application([])
    panel = Sps30StudyPanel(store)
    wanted = Sps30StudyParams.model_validate(
        {
            "setup": {"fan_enabled": False, "forward_axis": "+y", "min_exchange_flow_lpm": 1.5,
                      "domain_multipliers": {"upstream": 4, "downstream": 8, "lateral": 3, "vertical": 2}},
            "droplet_um": {"minimum": 5, "maximum": 500, "log": True, "distribution": "triangular", "mode": 50},
            "doe": "lhs", "doe_points": 30, "surrogate": "quadratic", "monte_carlo_samples": 3000,
        }
    )
    panel.apply_params(wanted)
    assert panel.study_params() == wanted
    panel.samples.setValue(1000)
    result = panel.run_analytic_now()
    assert panel.study_list.currentData() == result.study_id
    assert panel.points_table.rowCount() == len(result.points)
