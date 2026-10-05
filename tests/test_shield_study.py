"""Radiation-shield study: model, DoE, response surface, Monte Carlo, CFD, tools."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from backend import shield_study as study
from backend.runner import RunOutcome
from core.shield_models import (
    BottomMode,
    DesignPoint,
    Distribution,
    DoeKind,
    Evaluator,
    ShieldCfdSettings,
    ShieldSetup,
    ShieldStudyParams,
    StudyVariable,
    SurrogateKind,
    VariableScale,
)


def params(**changes) -> ShieldStudyParams:
    data = ShieldStudyParams().model_dump(mode="json")
    data.update(changes)
    return ShieldStudyParams.model_validate(data)


# ---------------------------------------------------------------------------
# Setup and the analytical model
# ---------------------------------------------------------------------------


def test_the_default_setup_is_the_specified_one():
    """20 cm shield, 2 x 2 x 1.44 m domain, 1000 W/m2 sun, 300 W/m2 ground."""
    setup = ShieldSetup()
    assert setup.shield_size_m == [0.2, 0.2, 0.2]
    assert setup.domain_size_m == [2.0, 2.0, 1.44]
    assert setup.solar_flux_w_m2 == 1000.0
    assert setup.bottom_flux_w_m2 == 300.0
    assert setup.bottom_temperature_k == 343.0


def test_a_750_w_roof_is_a_70_degree_roof():
    """The two ways the extended scenario is stated agree."""
    setup = ShieldSetup(bottom_mode="roof_temperature")
    assert setup.roof_temperature_for(750.0) == pytest.approx(343.5, abs=0.5)
    assert setup.roof_flux_for(343.0) == pytest.approx(747.0, abs=5.0)
    assert setup.baseline_bottom_flux() == pytest.approx(setup.roof_flux_for(343.0))


def test_the_thermometer_must_be_inside_the_shield():
    with pytest.raises(ValueError, match="inside the shield"):
        ShieldSetup(thermometer_xyz_m=[0.0, 0.0, 0.3])


def test_the_domain_must_leave_room_round_the_shield():
    with pytest.raises(ValueError, match="twice the shield"):
        ShieldSetup(domain_size_m=[0.3, 2.0, 1.44])


def test_a_range_must_be_ordered_and_a_log_range_positive():
    with pytest.raises(ValueError):
        StudyVariable(minimum=5.0, maximum=1.0)
    with pytest.raises(ValueError):
        StudyVariable(minimum=0.0, maximum=1.0, scale="log")


def test_the_error_grows_with_sun_and_bottom_radiation_and_falls_with_wind():
    """The physical trends the whole study rests on."""
    model = study.ShieldAnalyticModel(ShieldSetup())
    base = model.delta_t(1.0, 1000.0, 500.0)
    assert model.delta_t(1.0, 1200.0, 500.0) > base
    assert model.delta_t(1.0, 1000.0, 800.0) > base
    hot = model.delta_t(0.5, 1200.0, 800.0)
    assert abs(model.delta_t(5.0, 1200.0, 800.0)) < abs(hot)


def test_a_darker_shield_reads_warmer():
    light = study.ShieldAnalyticModel(ShieldSetup(shield_solar_absorptivity=0.1))
    dark = study.ShieldAnalyticModel(ShieldSetup(shield_solar_absorptivity=0.6))
    assert dark.delta_t(1.0, 1000.0, 300.0) > light.delta_t(1.0, 1000.0, 300.0)


# ---------------------------------------------------------------------------
# DoE, surface, Monte Carlo
# ---------------------------------------------------------------------------


def test_the_ccd_has_fifteen_distinct_points_inside_the_ranges():
    points = study.design_of_experiments(params())
    assert len(points) == 15
    inputs = np.array([p.inputs() for p in points])
    assert len({tuple(row) for row in inputs.round(6)}) == 15
    assert inputs[:, 0].min() == pytest.approx(0.5) and inputs[:, 0].max() == pytest.approx(5.0)
    # Wind is log-scaled: its centre point is the geometric mean.
    assert points[0].wind_speed_ms == pytest.approx(math.sqrt(0.5 * 5.0), rel=1e-5)
    assert points[0].solar_flux_w_m2 == pytest.approx(1000.0)


def test_a_latin_hypercube_has_the_asked_number_plus_the_centre():
    points = study.design_of_experiments(params(doe="lhs", doe_points=25))
    assert len(points) == 26
    inputs = np.array([p.inputs() for p in points])
    assert np.all(inputs[:, 1] >= 800.0) and np.all(inputs[:, 1] <= 1200.0)


def test_coding_round_trips():
    p = params()
    physical = np.array([[0.7, 900.0, 350.0], [4.0, 1100.0, 790.0]])
    assert study.decode(p, study.encode(p, physical)) == pytest.approx(physical)


def _solved(p: ShieldStudyParams, function) -> list[DesignPoint]:
    return study.solve_points(
        study.design_of_experiments(p), lambda point: function(*point.inputs()), "test"
    )


def test_a_quadratic_surface_reproduces_a_quadratic_exactly():
    p = params(wind_speed_ms={"minimum": 0.5, "maximum": 5.0})  # linear scale
    function = lambda w, s, b: 0.1 + 0.02 * w - 1e-4 * s + 3e-6 * b * b + 1e-5 * w * s  # noqa: E731
    surface = study.ResponseSurface(p, _solved(p, function))
    summary = surface.summary()
    assert summary.r_squared == pytest.approx(1.0)
    assert summary.loo_rmse_k < 1e-9
    assert surface.predict(np.array([[2.0, 950.0, 400.0]]))[0] == pytest.approx(
        function(2.0, 950.0, 400.0)
    )


def test_an_rbf_surface_passes_through_every_point():
    p = params(surrogate="rbf")
    points = _solved(p, study.ShieldAnalyticModel(p.setup).delta_t)
    surface = study.ResponseSurface(p, points)
    fitted = surface.predict(np.array([pt.inputs() for pt in points]))
    assert fitted == pytest.approx([pt.delta_t_k for pt in points], abs=1e-8)


def test_too_few_points_is_refused_with_a_reason():
    p = params()
    points = _solved(p, lambda w, s, b: 0.0)[:6]
    with pytest.raises(study.ShieldStudyError, match="at least 10"):
        study.ResponseSurface(p, points)


def test_monte_carlo_stays_in_range_and_is_repeatable():
    p = params(monte_carlo_samples=5000)
    rng = np.random.default_rng(3)
    samples = study.sample_inputs(p, 5000, rng)
    assert samples[:, 0].min() >= 0.5 and samples[:, 0].max() <= 5.0
    first = study.run_analytic_study(p).monte_carlo
    second = study.run_analytic_study(p).monte_carlo
    assert first == second
    assert 0.0 <= first.reliability <= 1.0
    assert first.abs_max_k >= first.abs_p95_k
    assert sum(first.histogram_counts) == 5000


def test_normal_and_triangular_inputs_are_truncated_to_the_range():
    p = params(
        solar_flux_w_m2={"minimum": 800, "maximum": 1200, "distribution": "normal", "std": 400},
        bottom_flux_w_m2={"minimum": 300, "maximum": 800, "distribution": "triangular", "mode": 750},
    )
    samples = study.sample_inputs(p, 20000, np.random.default_rng(1))
    assert samples[:, 1].min() >= 800 and samples[:, 1].max() <= 1200
    assert samples[:, 2].min() >= 300 and samples[:, 2].max() <= 800
    # The triangular peak pulls the mean towards it.
    assert samples[:, 2].mean() > 550.0


def test_the_worst_case_is_re_solved_and_close_to_the_surface():
    result = study.run_analytic_study(params(doe="lhs", doe_points=40))
    worst = result.monte_carlo.worst_case
    assert result.worst_case_check_k is not None
    assert result.worst_case_check_k == pytest.approx(worst["delta_t_k"], abs=0.25)
    # The worst case is the hot corner: low wind, strong bottom radiation.
    assert worst["wind_speed_ms"] < 1.5
    assert worst["bottom_flux_w_m2"] > 650.0
    assert result.monte_carlo.sensitivities["bottom_flux_w_m2"] > 0.5


# ---------------------------------------------------------------------------
# Design-point tables
# ---------------------------------------------------------------------------


def test_design_points_round_trip_through_csv(tmp_path):
    p = params()
    points = _solved(p, study.ShieldAnalyticModel(p.setup).delta_t)
    path = study.write_design_points_csv(tmp_path / "dp.csv", p.setup, points)
    back = study.read_design_points_csv(path, p.setup)
    assert [b.name for b in back] == [a.name for a in points]
    assert [b.delta_t_k for b in back] == pytest.approx([a.delta_t_k for a in points], rel=1e-5)


def test_a_workbench_export_is_understood(tmp_path):
    """DesignXplorer writes '#' comments and 'P1 - name [unit]' headers."""
    path = tmp_path / "workbench.csv"
    path.write_text(
        "# Design Points of Design of Experiments\n"
        "Name,P1 - wind_speed [m s^-1],P2 - solar_flux [W m^-2],"
        "P3 - bottom_flux [W m^-2],P4 - monitor_temperature [K]\n"
        "1,1.0,1000,300,298.40\n"
        "2,2.0,900,500,\n",
        encoding="utf-8",
    )
    setup = ShieldSetup(ambient_temp_c=25.0)
    points = study.read_design_points_csv(path, setup)
    assert len(points) == 2
    assert points[0].delta_t_k == pytest.approx(298.40 - 298.15)
    assert points[1].delta_t_k is None


def test_a_monitor_temperature_in_celsius_is_converted(tmp_path):
    path = tmp_path / "celsius.csv"
    path.write_text(
        "Name,P1 - wind_speed [m s^-1],P2 - solar_flux [W m^-2],"
        "P3 - bottom_flux [W m^-2],P4 - monitor_temperature [C]\n"
        "1,1.0,1000,300,25.40\n",
        encoding="utf-8",
    )
    points = study.read_design_points_csv(path, ShieldSetup(ambient_temp_c=25.0))
    assert points[0].delta_t_k == pytest.approx(0.40)


def test_a_table_without_inputs_is_refused(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("name,delta_t_k\nDP0,0.1\n", encoding="utf-8")
    with pytest.raises(study.ShieldStudyError, match="lacks the column"):
        study.read_design_points_csv(path, ShieldSetup())


# ---------------------------------------------------------------------------
# Radiation by ray casting
# ---------------------------------------------------------------------------


def _plate(z: float, size: float = 0.2, thickness: float = 0.004) -> np.ndarray:
    """A closed square slab as outward-facing triangles."""
    h = size / 2
    corners = np.array(
        [[x, y, zz] for zz in (z, z + thickness) for y in (-h, h) for x in (-h, h)]
    )
    quads = [
        (0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)
    ]
    triangles = []
    for a, b, c, d in quads:
        triangles += [corners[[a, b, c]], corners[[a, c, d]]]
    return np.array(triangles)


def test_an_upper_plate_shades_the_one_below():
    from backend.shield_cfd import _orient_triangles, radiation_facets

    triangles = _orient_triangles(np.concatenate([_plate(0.0), _plate(0.05)]))
    facets = radiation_facets(triangles, rays_per_face=64)
    centroids = triangles.mean(axis=1)
    up = facets.normals[:, 2] > 0.9
    top = up & (centroids[:, 2] > 0.05)
    lower = up & (centroids[:, 2] < 0.01)
    assert np.all(facets.sunlit[top] == 1.0)
    assert np.all(facets.sunlit[lower] == 0.0)
    assert facets.sky_view[top].mean() == pytest.approx(1.0)
    # The lower plate's top sees mostly the underside of the upper one.
    assert facets.sky_view[lower].mean() < 0.5
    down = facets.normals[:, 2] < -0.9
    bottom = down & (centroids[:, 2] < 0.01)
    assert facets.ground_view[bottom].mean() == pytest.approx(1.0)


def test_orientation_turns_every_normal_out_of_the_solid():
    from backend.shield_cfd import _normals, _orient_triangles

    slab = _plate(0.0)
    flipped = slab[:, [0, 2, 1]]
    fixed = _orient_triangles(flipped)
    centre = np.array([0.0, 0.0, 0.002])
    outward = np.einsum("ij,ij->i", _normals(fixed), fixed.mean(axis=1) - centre)
    assert np.all(outward > 0.0)


def test_absorbed_flux_counts_sun_sky_and_ground():
    from backend.shield_cfd import RadiationFacets

    facets = RadiationFacets(
        areas=np.ones(2),
        normals=np.array([[0.0, 0.0, 1.0], [0.0, 0.0, -1.0]]),
        sunlit=np.array([1.0, 0.0]),
        sky_view=np.array([1.0, 0.0]),
        ground_view=np.array([0.0, 1.0]),
    )
    setup = ShieldSetup(sky_longwave_w_m2=0.0, ground_albedo=0.0)
    absorbed = facets.absorbed(setup, 1000.0, 300.0)
    assert absorbed[0] == pytest.approx(0.2 * 1000.0)
    assert absorbed[1] == pytest.approx(0.9 * 300.0)


def test_radiation_is_linearised_about_the_wall_temperature():
    """h_r (T_eq - T) reproduces the net gain at T0 and its slope there."""
    from backend.shield_cfd import PreparedMesh, RadiationFacets, _marker_fluxes

    facets = RadiationFacets(
        areas=np.ones(2),
        normals=np.array([[0.0, 0.0, 1.0], [0.0, 0.0, -1.0]]),
        sunlit=np.array([1.0, 0.0]),
        sky_view=np.array([1.0, 0.0]),
        ground_view=np.array([0.0, 0.0]),
    )
    prepared = PreparedMesh(Path("x"), np.array([0, 1]), 2, facets, np.zeros((2, 3)), np.zeros((1, 3)))
    setup = ShieldSetup()
    wall = np.array([300.0, 300.0])
    absorbed = facets.absorbed(setup, 1000.0, 300.0)
    conditions = _marker_fluxes(setup, prepared, absorbed, wall)
    h, t_eq = conditions["SHIELD_1"]
    net = absorbed[0] - facets.emitted(setup, wall)[0]
    assert h * (t_eq - 300.0) == pytest.approx(net)
    assert h == pytest.approx(4 * setup.shield_emissivity * 5.670374419e-8 * 300.0**3)
    # A facet that sees neither sky nor ground keeps a plain heat flux.
    assert conditions["SHIELD_2"] == pytest.approx(0.0)


def test_facets_are_grouped_into_the_asked_classes():
    from backend.shield_cfd import classify

    labels = classify(np.linspace(0.0, 1.0, 100), 4)
    assert sorted(set(labels.tolist())) == [0, 1, 2, 3]
    assert classify(np.zeros(10), 4).tolist() == [0] * 10


def test_the_su2_config_carries_the_boundaries():
    from backend.shield_cfd import build_shield_config

    point = DesignPoint(name="DP1", wind_speed_ms=2.0, solar_flux_w_m2=1000, bottom_flux_w_m2=750)
    ground = build_shield_config(
        ShieldSetup(), ShieldCfdSettings(), point,
        {"SHIELD_1": 50.0, "SHIELD_2": -20.0, "SHIELD_3": (4.5, 301.25)}, 500, False,
    )
    assert "MARKER_INLET= ( INLET, 298.150000, 2.000000, 1.0, 0.0, 0.0 )" in ground
    assert "MARKER_OUTLET= ( OUTLET, 0.0 )" in ground
    assert "MARKER_HEATFLUX= ( SHIELD_1, 50.00000, SHIELD_2, -20.00000 )" in ground
    assert "MARKER_HEATTRANSFER= ( SHIELD_3, 4.500000, 301.25000 )" in ground
    assert "MARKER_SYM= ( TOP, SIDES, BOTTOM )" in ground
    assert "GRAVITY_FORCE= YES" in ground
    roof = build_shield_config(
        ShieldSetup(bottom_mode="roof_temperature"), ShieldCfdSettings(buoyancy=False),
        point, {"SHIELD_1": 0.0}, 500, True,
    )
    assert "MARKER_ISOTHERMAL= ( BOTTOM, 343.5" in roof
    assert "RESTART_SOL= YES" in roof
    assert "GRAVITY_FORCE" not in roof


class _TemperatureRunner:
    """Stands in for SU2: writes a volume solution warmer near the shield."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, config_path, working_directory, ranks=1, on_line=None, **_):
        import pyvista as pv

        working_directory = Path(working_directory)
        text = Path(config_path).read_text()
        self.calls.append(text)
        lines = (working_directory / "shield_domain.su2").read_text().splitlines()
        count = int(lines[1].split("=")[1])
        cells = np.array([[int(v) for v in lines[2 + i].split()[1:5]] for i in range(count)])
        start = 2 + count
        nodes = int(lines[start].split("=")[1])
        points = np.array(
            [[float(v) for v in lines[start + 1 + i].split()[:3]] for i in range(nodes)]
        )
        grid = pv.UnstructuredGrid(
            np.hstack([np.full((count, 1), 4), cells]).ravel(),
            np.full(count, pv.CellType.TETRA),
            points,
        )
        centre = np.array([0.0, 0.0, 0.72])
        distance = np.linalg.norm(points - centre, axis=1)
        grid.point_data["Temperature"] = 298.15 + 0.5 * np.exp(-distance / 0.1)
        # A wall heat flux with SU2's look: warmer walls lose heat to the air.
        grid.point_data["Heat_Flux"] = 6.0 * (grid.point_data["Temperature"] - 298.15)
        grid.save(str(working_directory / "flow.vtu"))
        (working_directory / "restart_flow.dat").write_text("restart")
        if on_line:
            on_line("|           1|  -5.0|  -4.0|  0.0|")
        return RunOutcome(return_code=0, lines=[], wall_time_s=0.1)


@pytest.fixture
def small_study(tmp_path, monkeypatch):
    """A quick, coarse CFD study on the built-in shield."""
    from backend import shield_cfd

    monkeypatch.setitem(shield_cfd.RESOLUTION_SIZES, "coarse", (0.012, 0.4))
    monkeypatch.setattr(shield_cfd, "RAY_SURFACE_SIZE", 0.04)
    setup = ShieldSetup(plate_count=3)
    cfd = ShieldCfdSettings(
        rays_per_face=16, radiation_classes=4, radiation_passes=2, solid_conduction="off"
    )
    points = [
        DesignPoint(name="DP0", wind_speed_ms=1.0, solar_flux_w_m2=1000, bottom_flux_w_m2=300)
    ]
    folder = shield_cfd.prepare_study(setup, cfd, points, tmp_path / "study")
    return folder


def test_a_prepared_study_has_everything_a_solve_needs(small_study):
    text = (small_study / "shield_domain.su2").read_text()
    for name in ("INLET", "OUTLET", "BOTTOM", "TOP", "SIDES", "SHIELD_1"):
        assert f"MARKER_TAG= {name}\n" in text
    assert "MARKER_TAG= SHIELD\n" not in text
    for name in ("study.json", "design_points.csv", "radiation_facets.npz",
                 "run_design_points.bat", "run_design_points.sh"):
        assert (small_study / name).is_file(), name
    geometry = json.loads((small_study / "geometry.json").read_text())
    assert geometry["shield_centre_m"] == pytest.approx([0.0, 0.0, 0.72])
    assert geometry["shield_size_m"][2] == pytest.approx(0.2, abs=0.01)


def test_a_design_point_runs_its_radiation_passes_and_reads_the_thermometer(small_study):
    from backend.shield_cfd import run_design_point, solved_points

    runner = _TemperatureRunner()
    point = DesignPoint(name="DP0", wind_speed_ms=1.0, solar_flux_w_m2=1000, bottom_flux_w_m2=300)
    result = run_design_point(small_study, point, runner)
    assert len(runner.calls) == 2
    assert "RESTART_SOL= NO" in runner.calls[0] and "RESTART_SOL= YES" in runner.calls[1]
    # Each pass re-linearises about the solved wall temperature, so the
    # wall conditions differ between the passes.
    flux = [line for line in runner.calls[0].splitlines() if line.startswith("MARKER_HEAT")]
    flux2 = [line for line in runner.calls[1].splitlines() if line.startswith("MARKER_HEAT")]
    assert flux != flux2
    # Interpolated between coarse nodes round the peak at the centre.
    assert 0.3 < result.delta_t_k <= 0.5
    assert solved_points(small_study)[0].delta_t_k == pytest.approx(result.delta_t_k)


# ---------------------------------------------------------------------------
# Stored studies, export and the MCP tool
# ---------------------------------------------------------------------------


def test_an_analytic_study_is_stored_and_reopened(store):
    from backend import shield_workflow

    result = shield_workflow.run_analytic(store, params(monte_carlo_samples=2000))
    assert result.study_id.startswith("shield-")
    assert shield_workflow.load_result(store, result.study_id) == result
    assert shield_workflow.load_params(store, result.study_id).monte_carlo_samples == 2000


def test_the_fluent_package_has_geometry_table_journal_and_readme(tmp_path):
    from backend.shield_export import export_fluent_package

    folder = export_fluent_package(params(setup={"plate_count": 3}), tmp_path / "fluent")
    for name in ("fluid_domain.step", "shield_solid.step", "design_points.csv",
                 "fluent_setup.jou", "README_FLUENT.md", "study.json"):
        assert (folder / name).is_file(), name
    journal = (folder / "fluent_setup.jou").read_text()
    assert "ke-standard" in journal and "do-model" in journal
    assert '"wind_speed"' in journal and "delta_t" in journal
    readme = (folder / "README_FLUENT.md").read_text()
    assert "Six Sigma" in readme and "2 x 2 x 1.44" in readme


def test_the_tool_runs_an_analytic_study(store):
    import mcp_server

    reply = mcp_server.radiation_shield_study(
        action="analytic",
        setup={"bottom_mode": "roof_temperature"},
        variables={"wind_speed_ms": {"minimum": 1.0, "maximum": 4.0}},
        study={"monte_carlo_samples": 1000},
    )
    assert reply["ok"], reply
    assert len(reply["design_points"]) == 15
    assert reply["monte_carlo"]["samples"] == 1000
    assert 1.0 <= reply["monte_carlo"]["worst_case"]["wind_speed_ms"] <= 4.0
    status = mcp_server.radiation_shield_study(action="status", study_id=reply["study_id"])
    assert status["ok"] and status["params"]["setup"]["bottom_mode"] == "roof_temperature"


def test_the_tool_imports_points_solved_elsewhere(store, tmp_path):
    import mcp_server

    p = params()
    points = _solved(p, study.ShieldAnalyticModel(p.setup).delta_t)
    path = study.write_design_points_csv(tmp_path / "solved.csv", p.setup, points)
    reply = mcp_server.radiation_shield_study(action="import", results_csv=str(path))
    assert reply["ok"], reply
    assert reply["evaluator"] == "imported"


def test_the_tool_explains_bad_input(store):
    import mcp_server

    bad = mcp_server.radiation_shield_study(
        action="analytic", variables={"wind_speed_ms": {"minimum": 6, "maximum": 1}}
    )
    assert bad["ok"] is False and "maximum" in bad["error"]
    unknown = mcp_server.radiation_shield_study(action="dance")
    assert unknown["ok"] is False and "unknown action" in unknown["error"]
    needs = mcp_server.radiation_shield_study(action="solve_cfd")
    assert needs["ok"] is False and "study_id" in needs["error"]


def test_only_the_cfd_actions_need_the_operators_approval():
    import mcp_server

    assert mcp_server.is_long_running("radiation_shield_study", {"action": "solve_cfd"})
    assert mcp_server.is_long_running("radiation_shield_study", {"action": "prepare_cfd"})
    assert not mcp_server.is_long_running("radiation_shield_study", {"action": "analytic"})
    assert mcp_server.is_long_running("run_parametric_sweep", {})


# ---------------------------------------------------------------------------
# What a solve reports back: per-point dT, radiation settling, oscillation
# ---------------------------------------------------------------------------


def _records(values):
    from backend.su2_parser import IterationRecord

    return [IterationRecord(iteration=i, values={"rms_pressure": v}) for i, v in enumerate(values)]


def test_a_steadily_falling_residual_is_not_oscillating():
    from backend.shield_cfd import residual_behaviour

    behaviour = residual_behaviour(_records([-1.0 - 0.01 * i for i in range(400)]))
    assert behaviour["oscillating"] is False
    assert behaviour["residual_drop_orders"] == pytest.approx(3.99, abs=0.01)


def test_a_swinging_flat_tail_is_flagged_as_oscillating():
    import math

    from backend.shield_cfd import residual_behaviour

    values = [-1.0 - 0.01 * i for i in range(200)] + [
        -3.0 + 0.6 * math.sin(i / 3.0) for i in range(200)
    ]
    assert residual_behaviour(_records(values))["oscillating"] is True


def test_a_solved_point_records_its_radiation_passes(small_study):
    from backend.shield_cfd import run_design_point

    point = DesignPoint(name="DP0", wind_speed_ms=1.0, solar_flux_w_m2=1000, bottom_flux_w_m2=300)
    result = run_design_point(small_study, point, _TemperatureRunner())
    saved = json.loads((small_study / "points" / "DP0" / "result.json").read_text())
    assert saved["wall_changes_k"] == result.wall_changes_k and len(result.wall_changes_k) == 2
    assert saved["radiation_settled"] == (result.wall_changes_k[-1] < 0.05)
    if not result.radiation_settled:
        assert any("radiation not settled" in note for note in result.notes)
    assert "oscillating" in saved and saved["mesh_resolution"] == "coarse"


def test_the_solve_reply_lists_every_point(store, monkeypatch, small_study):
    """The mesh test and the convergence checks need dT per point."""
    from backend import shield_workflow

    record = store.create("shield", {})
    target = record.path(shield_workflow.CFD_FOLDER)
    import shutil

    shutil.copytree(small_study, target)
    monkeypatch.setattr(shield_workflow, "load_params", lambda *_: ShieldStudyParams())
    summary = shield_workflow.solve_cfd(store, record.record_id, _TemperatureRunner(), max_points=1)
    assert summary["solved"] == 1
    row = summary["point_results"][0]
    assert row["name"] == "DP0" and row["delta_t_k"] is not None
    assert row["wall_changes_k"] and "radiation_settled" in row
    assert summary["points_solved_now"][0]["name"] == "DP0"
    assert set(summary["checks"]) == {"not_converged", "radiation_not_settled", "oscillating"}



# ---------------------------------------------------------------------------
# Conduction in the plates (partitioned conjugate heat transfer)
# ---------------------------------------------------------------------------


def test_the_solid_solver_matches_a_one_dimensional_slab():
    """Flux in on one face, convection out of the other: 1-D conduction."""
    import gmsh

    from backend.gmsh_session import gmsh_session
    from backend.shield_conduction import SolidConduction, SolidSurfaceState

    with gmsh_session("slab"):
        gmsh.model.occ.addBox(0, 0, 0, 0.05, 0.05, 0.01)
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", 0.005)
        gmsh.model.mesh.generate(3)
        tags, coords, _ = gmsh.model.mesh.getNodes()
        index = np.full(int(tags.max()) + 1, -1)
        index[tags.astype(int)] = np.arange(len(tags))
        _, _, nodes = gmsh.model.mesh.getElements(3)
        tets = index[nodes[0].astype(int)].reshape(-1, 4)
        points = coords.reshape(-1, 3)
    solid = SolidConduction(points, tets, 0.5)
    z = solid.centroids[:, 2]
    count = len(z)
    state = SolidSurfaceState(
        absorbed=np.where(z < 1e-6, 100.0, 0.0), emissivity=np.zeros(count),
        view=np.zeros(count), q_air=np.zeros(count), t_air_wall=np.full(count, 300.0),
        h_air=np.where(z > 0.01 - 1e-6, 10.0, 0.0),
    )
    temperature = solid.solve(state, 300.0)
    # q/h = 10 K across the film, q L / k = 2 K across the slab.
    assert temperature[points[:, 2] > 0.01 - 1e-9].mean() == pytest.approx(310.0, abs=1e-6)
    assert temperature[points[:, 2] < 1e-9].mean() == pytest.approx(312.0, abs=1e-6)


@pytest.fixture
def conjugate_study(tmp_path, monkeypatch):
    """A coarse study on the built-in shield with conduction in the plates."""
    from backend import shield_cfd

    monkeypatch.setitem(shield_cfd.RESOLUTION_SIZES, "coarse", (0.012, 0.4))
    monkeypatch.setattr(shield_cfd, "RAY_SURFACE_SIZE", 0.04)
    setup = ShieldSetup(plate_count=3, shield_conductivity_w_mk=167.0)
    cfd = ShieldCfdSettings(rays_per_face=16, radiation_classes=4, radiation_passes=4)
    points = [
        DesignPoint(name="DP0", wind_speed_ms=1.0, solar_flux_w_m2=800, bottom_flux_w_m2=300),
        DesignPoint(name="DP1", wind_speed_ms=1.0, solar_flux_w_m2=1200, bottom_flux_w_m2=300),
    ]
    return shield_cfd.prepare_study(setup, cfd, points, tmp_path / "study")


def test_conduction_carries_the_sun_through_the_plates(conjugate_study):
    """More sun on the top must warm the whole (conducting) shield."""
    from backend.shield_cfd import run_design_point

    assert (conjugate_study / "solid_mesh.npz").is_file()
    low = DesignPoint(name="DP0", wind_speed_ms=1.0, solar_flux_w_m2=800, bottom_flux_w_m2=300)
    high = DesignPoint(name="DP1", wind_speed_ms=1.0, solar_flux_w_m2=1200, bottom_flux_w_m2=300)
    runner = _TemperatureRunner()
    cooler = run_design_point(conjugate_study, low, runner)
    warmer = run_design_point(conjugate_study, high, runner)
    assert "MARKER_ISOTHERMAL=" in runner.calls[0]
    assert warmer.shield_mean_temp_k > cooler.shield_mean_temp_k + 0.1
    saved = json.loads((conjugate_study / "points" / "DP1" / "result.json").read_text())
    assert saved["conduction"] is True and saved["solid_temperature_k"]
    body = saved["solid_temperature_k"][0]
    assert body["min_k"] <= body["mean_k"] <= body["max_k"]
