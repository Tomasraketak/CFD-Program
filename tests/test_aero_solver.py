"""Aerodynamic solver: configuration, parsing, and the geometric reductions.

The hinge-torque and centre-of-pressure maths is checked against closed-form
cases, because those are the numbers an operator sizes a servo from and a
sign error would be both serious and invisible.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from backend.aero_solver import (
    AeroSolverError,
    center_of_pressure,
    dimensionalise,
    hinge_torque,
    run_aero_case,
    transfer_moment,
)
from backend.runner import FakeRunner, SolverFailedError
from backend.su2_config import (
    ConfigContext,
    build_aero_config,
    parse_config,
    select_convective_scheme,
    write_config,
)
from backend.su2_parser import (
    ConvergenceMonitor,
    SU2OutputParser,
    parse_forces_breakdown,
    parse_history_csv,
)
from core.models import (
    AeroRunRequest,
    ConvectiveScheme,
    FlowParams,
    HingeAxis,
    ReferenceValues,
    SolverParams,
    VelocityType,
)

# ---------------------------------------------------------------------------
# Fixtures: representative SU2 output
# ---------------------------------------------------------------------------

SCREEN_HEADER = (
    "|  Inner_Iter|    rms[Rho]|   rms[RhoE]|          CL|          CD|"
    "        CSF|        CMx|        CMy|        CMz|"
)


def screen_rows(count: int, final_residual: float = -6.0) -> list[str]:
    """Synthetic screen output converging smoothly to fixed coefficients."""
    lines = ["+" + "-" * 70 + "+", SCREEN_HEADER, "+" + "-" * 70 + "+"]
    for index in range(count):
        fraction = (index + 1) / count
        residual = -1.0 + (final_residual + 1.0) * fraction
        lines.append(
            "|{:12d}|{:12.6f}|{:12.6f}|{:12.6f}|{:12.6f}|{:12.6f}"
            "|{:12.6f}|{:12.6f}|{:12.6f}|".format(
                index, residual, residual - 1.0, 0.25, 0.42, 0.0, 0.0, -0.05, 0.0
            )
        )
    return lines


FORCES_BREAKDOWN = """
-------------------------------------------------------------------------
                      Forces breakdown
-------------------------------------------------------------------------
Total CL:       0.250000 | Pressure (  95%):   0.237500 | Friction (   5%): 0.012500
Total CD:       0.420000 | Pressure (  70%):   0.294000 | Friction (  30%): 0.126000
Total CSF:      0.000000 | Pressure (   0%):   0.000000 | Friction (   0%): 0.000000
Total CMx:      0.000000 | Pressure (   0%):   0.000000 | Friction (   0%): 0.000000
Total CMy:     -0.050000 | Pressure (  90%):  -0.045000 | Friction (  10%): -0.005000
Total CMz:      0.000000 | Pressure (   0%):   0.000000 | Friction (   0%): 0.000000
Total CFx:      0.410000 | Pressure (  70%):   0.287000 | Friction (  30%): 0.123000
Total CFy:      0.000000 | Pressure (   0%):   0.000000 | Friction (   0%): 0.000000
Total CFz:      0.260000 | Pressure (  95%):   0.247000 | Friction (   5%): 0.013000

Surface name: WALL_ROCKET
Total CL:       0.100000 | Pressure (  95%):   0.095000 | Friction (   5%): 0.005000
Total CD:       0.200000 | Pressure (  70%):   0.140000 | Friction (  30%): 0.060000
"""


@pytest.fixture
def mesh_file(tmp_path) -> Path:
    """A stand-in mesh file; the fake runner never reads it."""
    path = tmp_path / "mesh.su2"
    path.write_text("NDIME= 3\nNELEM= 0\nNPOIN= 0\nNMARK= 0\n")
    return path


def make_request(**overrides) -> AeroRunRequest:
    """An aero request with sensible defaults for tests."""
    return AeroRunRequest(
        mesh_id="mesh-test",
        flow=overrides.pop("flow", FlowParams(velocity_value=2.0, aoa_deg=5.0)),
        solver=overrides.pop(
            "solver", SolverParams(mpi_ranks=1, force_stabilization_window=10)
        ),
        reference=overrides.pop("reference", ReferenceValues()),
        hinge_axes=overrides.pop("hinge_axes", []),
    )


# ---------------------------------------------------------------------------
# Moment transfer and hinge torque
# ---------------------------------------------------------------------------


def test_transfer_moment_to_the_same_point_changes_nothing():
    """Transferring to the original point is the identity."""
    moment = np.array([1.0, 2.0, 3.0])
    force = np.array([10.0, 0.0, -5.0])
    origin = np.array([0.5, 0.0, 0.0])
    assert transfer_moment(moment, force, origin, origin) == pytest.approx(moment)


def test_transfer_moment_matches_the_closed_form():
    """A unit force offset by a unit arm produces the textbook moment.

    A +z force of 10 N applied 2 m aft of the origin gives M_y = -20 N m about
    the origin; referencing that moment back to the application point must
    leave zero.
    """
    application_point = np.array([2.0, 0.0, 0.0])
    force = np.array([0.0, 0.0, 10.0])
    moment_about_origin = np.cross(application_point, force)
    assert moment_about_origin == pytest.approx([0.0, -20.0, 0.0])

    at_application = transfer_moment(
        moment_about_origin, force, np.zeros(3), application_point
    )
    assert at_application == pytest.approx([0.0, 0.0, 0.0], abs=1e-12)


def test_hinge_torque_projects_onto_the_axis():
    """Only the moment component along the hinge drives rotation."""
    # 10 N in +z applied 0.1 m outboard in +y of a hinge at the origin.
    force = np.array([0.0, 0.0, 10.0])
    moment = np.cross(np.array([0.0, 0.1, 0.0]), force)  # = (1, 0, 0) N m

    along_x = HingeAxis(name="x", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])
    result = hinge_torque(moment, force, np.zeros(3), along_x)
    assert result.torque_nm == pytest.approx(1.0)
    assert result.moment_vector_nm == pytest.approx([1.0, 0.0, 0.0])

    # A hinge perpendicular to that moment feels no torque.
    along_y = HingeAxis(name="y", point=[0.0, 0.0, 0.0], direction=[0.0, 1.0, 0.0])
    assert hinge_torque(moment, force, np.zeros(3), along_y).torque_nm == pytest.approx(
        0.0, abs=1e-12
    )


def test_hinge_torque_uses_the_hinge_point_not_the_reference_origin():
    """The parallel-axis transfer is what makes an arbitrary hinge correct.

    A pure force through the hinge produces no hinge torque, however far the
    solver's moment reference origin is from it.
    """
    hinge_point = np.array([0.8, 0.05, 0.0])
    direction = np.array([0.0, 1.0, 0.0])
    force = np.array([0.0, 0.0, 25.0])

    # Force acts exactly on the hinge line, so torque about it must vanish.
    moment_about_origin = np.cross(hinge_point, force)
    hinge = HingeAxis(
        name="fin", point=list(hinge_point), direction=list(direction)
    )
    result = hinge_torque(moment_about_origin, force, np.zeros(3), hinge)
    assert result.torque_nm == pytest.approx(0.0, abs=1e-9)


def test_hinge_torque_sign_follows_the_right_hand_rule():
    """Reversing the axis direction reverses the reported torque."""
    force = np.array([0.0, 0.0, 10.0])
    moment = np.cross(np.array([0.0, 0.1, 0.0]), force)
    forward = HingeAxis(name="a", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])
    reverse = HingeAxis(name="b", point=[0.0, 0.0, 0.0], direction=[-1.0, 0.0, 0.0])
    assert hinge_torque(moment, force, np.zeros(3), forward).torque_nm == pytest.approx(
        -hinge_torque(moment, force, np.zeros(3), reverse).torque_nm
    )


def test_hinge_direction_need_not_be_normalised_by_the_caller():
    """Torque is unchanged by the magnitude of the supplied direction."""
    force = np.array([0.0, 0.0, 10.0])
    moment = np.cross(np.array([0.0, 0.1, 0.0]), force)
    unit = HingeAxis(name="a", point=[0.0, 0.0, 0.0], direction=[1.0, 0.0, 0.0])
    scaled = HingeAxis(name="a", point=[0.0, 0.0, 0.0], direction=[7.0, 0.0, 0.0])
    assert hinge_torque(moment, force, np.zeros(3), unit).torque_nm == pytest.approx(
        hinge_torque(moment, force, np.zeros(3), scaled).torque_nm
    )


# ---------------------------------------------------------------------------
# Centre of pressure
# ---------------------------------------------------------------------------


def test_center_of_pressure_recovers_a_known_application_point():
    """A normal force applied at a known station is located exactly."""
    station = 0.62
    force = np.array([0.0, 0.0, -40.0])  # normal force
    moment = np.cross(np.array([station, 0.0, 0.0]), force)

    cop = center_of_pressure(force, moment, np.zeros(3))
    assert cop[0] == pytest.approx(station)
    assert cop[1] == pytest.approx(0.0)
    assert cop[2] == pytest.approx(0.0)


def test_center_of_pressure_is_independent_of_the_reference_origin():
    """Shifting the moment reference does not move the physical CoP."""
    station = 0.62
    force = np.array([0.0, 0.0, -40.0])
    application = np.array([station, 0.0, 0.0])

    for origin in (np.zeros(3), np.array([0.3, 0.0, 0.0]), np.array([-1.0, 0.0, 0.0])):
        moment = np.cross(application - origin, force)
        cop = center_of_pressure(force, moment, origin)
        assert cop[0] == pytest.approx(station)


def test_center_of_pressure_is_undefined_without_a_normal_force():
    """At zero incidence there is no transverse force, so no CoP exists.

    Reporting NaN is the honest answer; returning a number would invite a
    reader to trust a meaningless station.
    """
    cop = center_of_pressure(
        np.array([100.0, 0.0, 0.0]), np.zeros(3), np.zeros(3)
    )
    assert math.isnan(cop[0])


def test_center_of_pressure_uses_the_side_force_when_it_dominates():
    """A yawed case is located from the better-conditioned component."""
    station = 0.4
    force = np.array([0.0, 30.0, 0.0])
    moment = np.cross(np.array([station, 0.0, 0.0]), force)
    assert center_of_pressure(force, moment, np.zeros(3))[0] == pytest.approx(station)


# ---------------------------------------------------------------------------
# Dimensionalisation
# ---------------------------------------------------------------------------


def test_dimensionalise_scales_by_dynamic_pressure_and_area():
    """Forces scale with q*S and moments additionally with the length."""
    coefficients = {"cd": 0.5, "cl": 0.2, "cs": 0.0, "cmy": -0.1, "cfx": 0.5}
    forces = dimensionalise(coefficients, 1000.0, 2.0, 0.5)
    assert forces.drag_n == pytest.approx(1000.0)
    assert forces.lift_n == pytest.approx(400.0)
    assert forces.force[0] == pytest.approx(1000.0)
    assert forces.moment[1] == pytest.approx(-0.1 * 1000.0 * 2.0 * 0.5)


def test_dimensionalise_defaults_missing_coefficients_to_zero():
    """A partially reported coefficient set does not produce NaN."""
    forces = dimensionalise({"cd": 0.3}, 500.0, 1.0, 1.0)
    assert forces.lift_n == pytest.approx(0.0)
    assert np.all(np.isfinite(forces.moment))


# ---------------------------------------------------------------------------
# Configuration generation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mach,expected",
    [
        (0.2, ConvectiveScheme.JST),
        (0.79, ConvectiveScheme.JST),
        (0.8, ConvectiveScheme.ROE),
        (2.0, ConvectiveScheme.ROE),
        (3.5, ConvectiveScheme.ROE),
    ],
)
def test_regime_selects_the_convective_scheme(mach, expected):
    """Central differencing subsonically, upwind once shocks appear."""
    assert select_convective_scheme(mach) is expected


def test_scheme_override_is_respected():
    """An explicit scheme wins over the automatic choice."""
    assert (
        select_convective_scheme(0.2, ConvectiveScheme.AUSM) is ConvectiveScheme.AUSM
    )


def base_context() -> ConfigContext:
    """A reference configuration context."""
    return ConfigContext(
        mesh_filename="mesh.su2",
        reference_area_m2=0.005,
        reference_length_m=0.08,
        moment_origin=(0.0, 0.0, 0.0),
        wall_markers=("WALL_ROCKET", "WALL_FINS"),
    )


def test_subsonic_config_uses_jst_without_a_limiter():
    """JST needs dissipation coefficients, not MUSCL reconstruction."""
    flow = FlowParams(velocity_value=0.3)
    settings = parse_config(build_aero_config(flow, SolverParams(), base_context()))
    assert settings["CONV_NUM_METHOD_FLOW"] == "JST"
    assert "JST_SENSOR_COEFF" in settings
    assert "SLOPE_LIMITER_FLOW" not in settings


def test_supersonic_config_uses_muscl_and_venkatakrishnan():
    """The supersonic branch matches the specified numerics."""
    flow = FlowParams(velocity_value=2.5)
    settings = parse_config(build_aero_config(flow, SolverParams(), base_context()))
    assert settings["CONV_NUM_METHOD_FLOW"] == "ROE"
    assert settings["MUSCL_FLOW"] == "YES"
    assert settings["SLOPE_LIMITER_FLOW"] == "VENKATAKRISHNAN"
    # The entropy fix keeps Roe from admitting expansion shocks.
    assert "ENTROPY_FIX_COEFF" in settings


def test_config_carries_flow_conditions_and_attitude():
    """Mach, alpha, beta and the freestream state reach the config."""
    flow = FlowParams(velocity_value=1.8, aoa_deg=-7.5, sideslip_deg=3.25)
    settings = parse_config(build_aero_config(flow, SolverParams(), base_context()))
    assert float(settings["MACH_NUMBER"]) == pytest.approx(1.8)
    assert float(settings["AOA"]) == pytest.approx(-7.5)
    assert float(settings["SIDESLIP_ANGLE"]) == pytest.approx(3.25)
    assert float(settings["FREESTREAM_PRESSURE"]) == pytest.approx(101325.0, rel=1e-3)


def test_config_uses_true_airspeed_correctly():
    """A TAS-specified case yields the equivalent Mach number."""
    flow = FlowParams(velocity_type=VelocityType.TAS, velocity_value=340.294)
    settings = parse_config(build_aero_config(flow, SolverParams(), base_context()))
    assert float(settings["MACH_NUMBER"]) == pytest.approx(1.0, rel=1e-4)


def test_config_sets_the_moment_origin_and_references():
    """REF_ORIGIN_MOMENT drives every moment SU2 reports."""
    context = base_context()
    context.moment_origin = (0.85, 0.04, -0.01)
    settings = parse_config(
        build_aero_config(FlowParams(velocity_value=1.0), SolverParams(), context)
    )
    assert float(settings["REF_ORIGIN_MOMENT_X"]) == pytest.approx(0.85)
    assert float(settings["REF_ORIGIN_MOMENT_Y"]) == pytest.approx(0.04)
    assert float(settings["REF_ORIGIN_MOMENT_Z"]) == pytest.approx(-0.01)
    assert float(settings["REF_AREA"]) == pytest.approx(0.005)


def test_config_declares_markers_and_wall_functions():
    """Wall, farfield and monitoring markers match the mesh tags."""
    text = build_aero_config(
        FlowParams(velocity_value=1.0), SolverParams(), base_context()
    )
    settings = parse_config(text)
    assert "WALL_ROCKET" in settings["MARKER_HEATFLUX"]
    assert "WALL_FINS" in settings["MARKER_HEATFLUX"]
    assert "FARFIELD" in settings["MARKER_FAR"]
    # Wall functions are what make the y+ 30-60 mesh valid.
    assert "STANDARD_WALL_FUNCTION" in settings["MARKER_WALL_FUNCTIONS"]
    assert "WALL_ROCKET" in settings["MARKER_MONITORING"]


def test_config_applies_solver_settings():
    """Turbulence model, CFL and iteration cap come from the model."""
    solver = SolverParams(
        turbulence_model="SA", cfl_number=12.5, max_iterations=1234,
        convergence_residual=-8.0,
    )
    settings = parse_config(
        build_aero_config(FlowParams(velocity_value=1.0), solver, base_context())
    )
    assert settings["KIND_TURB_MODEL"] == "SA"
    assert float(settings["CFL_NUMBER"]) == pytest.approx(12.5)
    assert int(settings["ITER"]) == 1234
    assert float(settings["CONV_RESIDUAL_MINVAL"]) == pytest.approx(-8.0)


def test_symmetry_marker_only_appears_when_requested():
    """A half-domain mesh declares SYMMETRY; a full one must not."""
    context = base_context()
    full = parse_config(
        build_aero_config(FlowParams(velocity_value=1.0), SolverParams(), context)
    )
    assert "MARKER_SYM" not in full

    context.has_symmetry = True
    half = parse_config(
        build_aero_config(FlowParams(velocity_value=1.0), SolverParams(), context)
    )
    assert "SYMMETRY" in half["MARKER_SYM"]


def test_written_config_has_unix_line_endings(tmp_path):
    """Stray carriage returns break some SU2 builds' parser."""
    text = build_aero_config(
        FlowParams(velocity_value=1.0), SolverParams(), base_context()
    )
    path = write_config(text, tmp_path / "solver.cfg")
    assert b"\r\n" not in path.read_bytes()


# ---------------------------------------------------------------------------
# Result file parsing
# ---------------------------------------------------------------------------


def test_parse_forces_breakdown_reads_total_coefficients(tmp_path):
    """The whole-body totals are read, not the per-surface blocks."""
    path = tmp_path / "forces_breakdown.dat"
    path.write_text(FORCES_BREAKDOWN)
    coefficients = parse_forces_breakdown(path)

    assert coefficients["cl"] == pytest.approx(0.25)
    assert coefficients["cd"] == pytest.approx(0.42)
    assert coefficients["cmy"] == pytest.approx(-0.05)
    assert coefficients["cfz"] == pytest.approx(0.26)


def test_parse_forces_breakdown_ignores_later_surface_blocks(tmp_path):
    """A per-marker CD of 0.2 must not overwrite the total of 0.42."""
    path = tmp_path / "forces_breakdown.dat"
    path.write_text(FORCES_BREAKDOWN)
    assert parse_forces_breakdown(path)["cd"] == pytest.approx(0.42)


def test_parse_forces_breakdown_missing_file(tmp_path):
    """A missing breakdown file is reported clearly."""
    with pytest.raises(FileNotFoundError):
        parse_forces_breakdown(tmp_path / "absent.dat")


def test_parse_history_csv(tmp_path):
    """History columns are mapped onto canonical names."""
    path = tmp_path / "history.csv"
    path.write_text('"Inner_Iter","rms[Rho]","CL","CD"\n0,-1.0,0.1,0.5\n1,-2.0,0.2,0.4\n')
    history = parse_history_csv(path)
    assert history["iteration"].tolist() == [0.0, 1.0]
    assert history["rms_rho"].tolist() == [-1.0, -2.0]
    assert history["cd"][-1] == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# Screen parsing and convergence
# ---------------------------------------------------------------------------


def test_parser_reads_the_screen_table():
    """Columns are located by name, not by position."""
    parser = SU2OutputParser()
    for line in screen_rows(5):
        parser.feed(line)
    assert parser.columns[0] == "iteration"
    assert len(parser.records) == 5
    assert parser.final_value("cd") == pytest.approx(0.42)
    assert parser.final_value("cmy") == pytest.approx(-0.05)


def test_parser_ignores_banner_and_blank_lines():
    """Decorative output must not be mistaken for data."""
    parser = SU2OutputParser()
    for line in ("", "+-----+", "| ==== |", "Some prose about the solver."):
        assert parser.feed(line) is None
    assert parser.records == []


def test_parser_records_solver_errors():
    """Error lines are captured rather than parsed as data."""
    parser = SU2OutputParser()
    parser.feed("Error in su2 config file: unknown option")
    assert parser.errors


def test_convergence_monitor_stops_on_the_residual_threshold():
    """Falling the requested number of orders ends the run."""
    parser = SU2OutputParser()
    for line in screen_rows(100, final_residual=-7.0):
        parser.feed(line)
    monitor = ConvergenceMonitor(residual_threshold=-5.0, minimum_iterations=10)
    assert monitor.should_stop(parser)
    assert "residual" in (monitor.reason or "")


def test_convergence_monitor_waits_for_the_minimum_iterations():
    """An early transient must not be mistaken for convergence."""
    parser = SU2OutputParser()
    for line in screen_rows(5, final_residual=-9.0):
        parser.feed(line)
    monitor = ConvergenceMonitor(residual_threshold=-5.0, minimum_iterations=50)
    assert not monitor.should_stop(parser)


def test_convergence_monitor_stops_on_steady_forces():
    """Steady forces end the run even with the residual still high.

    A RANS solve often reaches usable forces well before the residual target,
    and the forces are what the operator actually wants.
    """
    parser = SU2OutputParser()
    for line in screen_rows(60, final_residual=-2.0):
        parser.feed(line)
    monitor = ConvergenceMonitor(
        residual_threshold=-12.0, force_window=20, force_tolerance=1e-3,
        minimum_iterations=10,
    )
    assert monitor.should_stop(parser)
    assert "steady" in (monitor.reason or "")


def test_convergence_monitor_detects_divergence():
    """A residual climbing away from its best value is not recoverable."""
    parser = SU2OutputParser()
    lines = ["+---+", SCREEN_HEADER, "+---+"]
    for index in range(40):
        residual = -6.0 + index * 0.5  # climbing
        lines.append(
            "|{:12d}|{:12.6f}|{:12.6f}|{:12.6f}|{:12.6f}|{:12.6f}"
            "|{:12.6f}|{:12.6f}|{:12.6f}|".format(
                index, residual, residual, 0.1, 0.2, 0.0, 0.0, 0.0, 0.0
            )
        )
    for line in lines:
        parser.feed(line)
    assert ConvergenceMonitor().diverged(parser)


# ---------------------------------------------------------------------------
# End-to-end run against the fake solver
# ---------------------------------------------------------------------------


def test_run_aero_case_produces_a_complete_result(mesh_file, tmp_path):
    """A full case reduces recorded output to forces, CoP and torques."""
    runner = FakeRunner(
        lines=screen_rows(120),
        artifacts={"forces_breakdown.dat": FORCES_BREAKDOWN},
    )
    request = make_request(
        hinge_axes=[
            HingeAxis(name="fin_pitch", point=[0.9, 0.05, 0.0], direction=[0, 1, 0])
        ]
    )
    result = run_aero_case(
        request,
        mesh_path=mesh_file,
        working_directory=tmp_path / "run",
        runner=runner,
        reference_area_m2=0.005,
        reference_length_m=0.08,
        sim_id="aero-test",
    )

    assert result.sim_id == "aero-test"
    assert result.cd == pytest.approx(0.42)
    assert result.cl == pytest.approx(0.25)
    assert result.mach == pytest.approx(2.0)

    # Dimensional forces follow from q * S.
    q = result.dynamic_pressure_pa
    assert result.drag_n == pytest.approx(0.42 * q * 0.005)
    assert result.lift_n == pytest.approx(0.25 * q * 0.005)

    assert len(result.hinge_torques) == 1
    assert result.hinge_torques[0].name == "fin_pitch"
    assert math.isfinite(result.hinge_torques[0].torque_nm)
    assert result.wall_time_s >= 0.0


def test_run_aero_case_writes_a_config_the_solver_receives(mesh_file, tmp_path):
    """The config is written into the run directory and passed to the solver."""
    runner = FakeRunner(
        lines=screen_rows(60), artifacts={"forces_breakdown.dat": FORCES_BREAKDOWN}
    )
    work = tmp_path / "run"
    run_aero_case(
        make_request(),
        mesh_path=mesh_file,
        working_directory=work,
        runner=runner,
        reference_area_m2=0.005,
        reference_length_m=0.08,
    )

    config = work / "solver.cfg"
    assert config.is_file()
    assert runner.calls[0]["config_path"] == str(config)
    # The mesh is copied in, because SU2 resolves it relative to its own cwd.
    assert (work / mesh_file.name).is_file()
    assert parse_config(config.read_text())["MESH_FILENAME"] == mesh_file.name


def test_run_aero_case_streams_iterations_for_live_plotting(mesh_file, tmp_path):
    """Iteration records arrive during the run, not only at the end."""
    seen: list[int] = []
    runner = FakeRunner(
        lines=screen_rows(40), artifacts={"forces_breakdown.dat": FORCES_BREAKDOWN}
    )
    run_aero_case(
        make_request(),
        mesh_path=mesh_file,
        working_directory=tmp_path / "run",
        runner=runner,
        reference_area_m2=0.005,
        reference_length_m=0.08,
        on_iteration=lambda record: seen.append(record.iteration),
    )
    assert seen == list(range(len(seen)))
    assert len(seen) > 1


def test_run_aero_case_stops_early_once_converged(mesh_file, tmp_path):
    """Early termination is the point of the convergence monitor."""
    runner = FakeRunner(
        lines=screen_rows(5000, final_residual=-9.0),
        artifacts={"forces_breakdown.dat": FORCES_BREAKDOWN},
    )
    result = run_aero_case(
        make_request(
            # A tolerance no run can meet isolates the residual criterion.
            solver=SolverParams(
                mpi_ranks=1,
                convergence_residual=-5.0,
                force_stabilization_tol=1.0e-15,
            )
        ),
        mesh_path=mesh_file,
        working_directory=tmp_path / "run",
        runner=runner,
        reference_area_m2=0.005,
        reference_length_m=0.08,
    )
    assert result.converged
    assert result.iterations < 4999


def test_run_aero_case_falls_back_to_screen_coefficients(mesh_file, tmp_path):
    """Without a breakdown file, the screen table still yields results."""
    runner = FakeRunner(lines=screen_rows(80))
    result = run_aero_case(
        make_request(),
        mesh_path=mesh_file,
        working_directory=tmp_path / "run",
        runner=runner,
        reference_area_m2=0.005,
        reference_length_m=0.08,
    )
    assert result.cd == pytest.approx(0.42)
    assert result.cl == pytest.approx(0.25)


def test_run_aero_case_rejects_a_missing_mesh(tmp_path):
    """A bad mesh path is caught before the solver is launched."""
    with pytest.raises(AeroSolverError, match="mesh file not found"):
        run_aero_case(
            make_request(),
            mesh_path=tmp_path / "absent.su2",
            working_directory=tmp_path / "run",
            runner=FakeRunner(lines=[]),
            reference_area_m2=0.005,
            reference_length_m=0.08,
        )


def test_run_aero_case_reports_solver_errors(mesh_file, tmp_path):
    """An SU2 error message surfaces instead of an empty result."""
    runner = FakeRunner(lines=[*screen_rows(20), "Error in CConfig: bad marker"])
    with pytest.raises(AeroSolverError, match="SU2 reported an error"):
        run_aero_case(
            make_request(),
            mesh_path=mesh_file,
            working_directory=tmp_path / "run",
            runner=runner,
            reference_area_m2=0.005,
            reference_length_m=0.08,
        )


def test_run_aero_case_reports_a_run_that_produced_nothing(mesh_file, tmp_path):
    """No parsed history means the run never really started."""
    runner = FakeRunner(lines=["SU2 banner", "some prose"])
    with pytest.raises(AeroSolverError, match="no iteration history"):
        run_aero_case(
            make_request(),
            mesh_path=mesh_file,
            working_directory=tmp_path / "run",
            runner=runner,
            reference_area_m2=0.005,
            reference_length_m=0.08,
        )


def test_failed_solver_exit_is_raised(mesh_file, tmp_path):
    """A non-zero exit status is an error, with the output attached."""
    runner = FakeRunner(lines=["boom"], return_code=1)
    with pytest.raises(SolverFailedError) as excinfo:
        runner.run(config_path=Path("x.cfg"), working_directory=tmp_path)
    assert excinfo.value.return_code == 1
    assert "boom" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Wind axes to the mesh frame
# ---------------------------------------------------------------------------


def test_drag_at_zero_incidence_points_downstream():
    """The frame check that catches a sign error in one line.

    Meshing puts the nose at minimum X with the flow running in +X, so at
    zero incidence the whole force is drag along +X. The classical body-axis
    convention has the x axis pointing forward out of the nose and returns
    the negative of this; combined with SU2's moments, which arrive in the
    mesh frame, that puts the centre of pressure on the wrong side.
    """
    from backend.aero_solver import _body_forces_from_wind_axes

    force = _body_forces_from_wind_axes(
        100.0, 0.0, 0.0, FlowParams(velocity_value=2.0, aoa_deg=0.0, sideslip_deg=0.0)
    )
    assert force[0] == pytest.approx(100.0)
    assert force[1] == pytest.approx(0.0)
    assert force[2] == pytest.approx(0.0)


def test_lift_at_zero_incidence_points_up():
    """Lift is +Z when the body is level, whatever the drag is doing."""
    from backend.aero_solver import _body_forces_from_wind_axes

    force = _body_forces_from_wind_axes(
        0.0, 50.0, 0.0, FlowParams(velocity_value=2.0, aoa_deg=0.0, sideslip_deg=0.0)
    )
    assert force[2] == pytest.approx(50.0)
    assert force[0] == pytest.approx(0.0)


@pytest.mark.parametrize(
    "aoa, sideslip", [(0.0, 0.0), (5.0, 0.0), (0.0, 4.0), (12.0, -6.0)]
)
def test_the_wind_to_mesh_conversion_is_a_rotation(aoa, sideslip):
    """Changing frames must not change how hard the air is pushing."""
    from backend.aero_solver import _body_forces_from_wind_axes

    drag, lift, side = 120.0, 40.0, 15.0
    force = _body_forces_from_wind_axes(
        drag, lift, side, FlowParams(velocity_value=2.0, aoa_deg=aoa, sideslip_deg=sideslip)
    )
    assert float(np.linalg.norm(force)) == pytest.approx(
        math.sqrt(drag**2 + lift**2 + side**2)
    )


def test_incidence_tilts_the_drag_the_way_the_freestream_leans():
    """At positive angle of attack the flow arrives from below and behind."""
    from backend.aero_solver import _body_forces_from_wind_axes

    force = _body_forces_from_wind_axes(
        100.0, 0.0, 0.0, FlowParams(velocity_value=2.0, aoa_deg=10.0, sideslip_deg=0.0)
    )
    assert force[0] == pytest.approx(100.0 * math.cos(math.radians(10.0)))
    assert force[2] == pytest.approx(100.0 * math.sin(math.radians(10.0)))


# ---------------------------------------------------------------------------
# Divergence detection and the automatic rescue
# ---------------------------------------------------------------------------


def diverging_rows(good: int = 5) -> list[str]:
    """Screen output that blows up, the way the operator's Mach 1.3 run did.

    A few healthy iterations, a NaN row, then SU2's own announcement.
    """
    lines = screen_rows(good, final_residual=-1.5)
    lines.append(
        "|{:12d}|{:>12}|{:>12}|{:>12}|{:>12}|{:>12}"
        "|{:>12}|{:>12}|{:>12}|".format(
            good, "nan", "nan", "nan", "nan", "nan", "nan", "nan", "nan"
        )
    )
    lines.append(
        'Error in "void CSolver::SetResidual_RMS(CGeometry *, CConfig *)": '
    )
    lines.append("SU2 has diverged (NaN detected).")
    return lines


def test_nan_rows_are_recorded_rather_than_discarded():
    """A blown-up iteration is data; dropping it hid the blow-up entirely.

    The numeric pattern used to reject 'nan', so the row vanished and the
    last good value stood in for it -- which is why divergence detection
    could never fire.
    """
    parser = SU2OutputParser()
    for line in diverging_rows(good=3):
        parser.feed(line)

    assert len(parser.records) == 4
    assert not math.isfinite(parser.records[-1].rms_rho)
    # The most recent *finite* value is still what final_value reports: that
    # contract is right for reporting and must not change.
    assert math.isfinite(parser.final_value("rms_rho"))


def test_a_divergence_message_is_not_a_hard_error():
    """SU2's NaN announcement must not land in errors.

    Anything in errors aborts the run before the rescue can start, which
    would make the whole recovery path unreachable.
    """
    parser = SU2OutputParser()
    for line in diverging_rows():
        parser.feed(line)

    assert parser.divergence_messages
    assert not parser.errors
    assert ConvergenceMonitor().diverged(parser)


def test_solver_warnings_are_kept_rather_than_swallowed():
    """A y+ warning is the solver telling the operator about their mesh."""
    parser = SU2OutputParser()
    parser.feed(
        "Warning: y+ < 5 in 329 points, for which the wall model is not active."
    )
    assert parser.warnings
    assert not parser.errors


def test_a_diverging_run_is_stopped_within_a_few_iterations(tmp_path, mesh_file):
    """A blow-up ends the run at once instead of grinding to ITER= 5000."""
    lines = diverging_rows(good=5) + screen_rows(400)
    runner = FakeRunner(lines=lines)
    request = make_request(
        solver=SolverParams(
            mpi_ranks=1, force_stabilization_window=10, rescue_on_divergence=False
        )
    )

    with pytest.raises(AeroSolverError, match="diverged"):
        run_aero_case(
            request=request,
            mesh_path=mesh_file,
            working_directory=tmp_path / "run",
            runner=runner,
            reference_area_m2=0.01,
            reference_length_m=0.1,
        )
    # Six rows parsed, then stopped: nowhere near the 400 that followed.
    assert len(runner.calls) == 1


def test_the_rescue_runs_on_divergence_and_reports_itself(tmp_path, mesh_file):
    """A diverged solve is retried first order, then restarted second order.

    The result is valid, but the operator must be told it needed help --
    otherwise a rescued number looks exactly like one that converged first
    time.
    """
    runner = FakeRunner(
        scripted=[
            {"lines": diverging_rows(good=5)},
            # Stage A: first order, must run to its cap and leave a restart.
            {
                "lines": screen_rows(40, final_residual=-3.0),
                "artifacts": {"restart_flow.dat": "binary-ish restart"},
            },
            # Stage B: second order from that restart.
            {
                "lines": screen_rows(60, final_residual=-6.0),
                "artifacts": {"forces_breakdown.dat": FORCES_BREAKDOWN},
            },
        ]
    )
    result = run_aero_case(
        request=make_request(),
        mesh_path=mesh_file,
        working_directory=tmp_path / "run",
        runner=runner,
        reference_area_m2=0.01,
        reference_length_m=0.1,
    )

    assert len(runner.calls) == 3
    stage_a = parse_config(str(runner.calls[1]["config_text"]))
    assert stage_a["MUSCL_FLOW"] == "NO"
    assert stage_a["CONV_NUM_METHOD_FLOW"] == "ROE"
    assert stage_a["CONV_FILENAME"] == "history_firstorder"

    stage_b = parse_config(str(runner.calls[2]["config_text"]))
    assert stage_b["RESTART_SOL"] == "YES"
    assert stage_b["SOLUTION_FILENAME"] == "solution_flow.dat"
    assert stage_b["MUSCL_FLOW"] == "YES"

    # The restart really was handed over, under its own name.
    assert (tmp_path / "run" / "solution_flow.dat").is_file()

    assert result.rescued
    assert result.notes and "diverged" in result.notes[0]
    assert result.cd == pytest.approx(0.42)


def test_a_healthy_run_is_left_completely_alone(tmp_path, mesh_file):
    """One invocation, no rescue: the untouched path must stay untouched."""
    runner = FakeRunner(
        lines=screen_rows(60),
        artifacts={"forces_breakdown.dat": FORCES_BREAKDOWN},
    )
    result = run_aero_case(
        request=make_request(),
        mesh_path=mesh_file,
        working_directory=tmp_path / "run",
        runner=runner,
        reference_area_m2=0.01,
        reference_length_m=0.1,
    )
    assert len(runner.calls) == 1
    assert not result.rescued
    assert not result.notes


def test_a_rescue_is_not_attempted_when_it_is_turned_off(tmp_path, mesh_file):
    """Turning the rescue off must fail loudly, not retry anyway."""
    runner = FakeRunner(lines=diverging_rows())
    request = make_request(
        solver=SolverParams(mpi_ranks=1, rescue_on_divergence=False)
    )
    with pytest.raises(AeroSolverError, match="rescue is disabled"):
        run_aero_case(
            request=request,
            mesh_path=mesh_file,
            working_directory=tmp_path / "run",
            runner=runner,
            reference_area_m2=0.01,
            reference_length_m=0.1,
        )
    assert len(runner.calls) == 1


def test_a_jst_case_is_rescued_at_genuinely_first_order_and_low_cfl(
    tmp_path, mesh_file
):
    """The Mach 0.7 failure: the rescue repeated the scheme that had failed.

    Turning MUSCL off does nothing to JST, and a start of CFL 0.5 on a
    doubling ramp is back at 100 in eight iterations. Stage A must switch to
    first-order Roe and hold the CFL down; stage B returns to JST.
    """
    runner = FakeRunner(
        scripted=[
            {"lines": diverging_rows(good=5)},
            {
                "lines": screen_rows(40, final_residual=-3.0),
                "artifacts": {"restart_flow.dat": "restart"},
            },
            {
                "lines": screen_rows(60, final_residual=-6.0),
                "artifacts": {"forces_breakdown.dat": FORCES_BREAKDOWN},
            },
        ]
    )
    result = run_aero_case(
        request=make_request(flow=FlowParams(velocity_value=0.7)),
        mesh_path=mesh_file,
        working_directory=tmp_path / "run",
        runner=runner,
        reference_area_m2=0.01,
        reference_length_m=0.1,
    )

    first = parse_config(str(runner.calls[0]["config_text"]))
    assert first["CONV_NUM_METHOD_FLOW"] == "JST"

    stage_a = parse_config(str(runner.calls[1]["config_text"]))
    assert stage_a["CONV_NUM_METHOD_FLOW"] == "ROE"
    assert stage_a["MUSCL_FLOW"] == "NO"
    assert float(stage_a["CFL_NUMBER"]) == pytest.approx(0.5)
    assert stage_a["CFL_ADAPT_PARAM"] == "( 0.5, 1.05, 0.1, 10 )"

    stage_b = parse_config(str(runner.calls[2]["config_text"]))
    assert stage_b["CONV_NUM_METHOD_FLOW"] == "JST"
    assert stage_b["RESTART_SOL"] == "YES"
    assert result.rescued
    assert "first-order Roe" in result.notes[0]


def test_a_rescue_that_diverges_too_gives_up_instead_of_looping(
    tmp_path, mesh_file
):
    """Two divergences end the attempt, with an honest account of both."""
    runner = FakeRunner(
        scripted=[{"lines": diverging_rows()}, {"lines": diverging_rows()}]
    )
    with pytest.raises(AeroSolverError, match="diverged twice") as caught:
        run_aero_case(
            request=make_request(),
            mesh_path=mesh_file,
            working_directory=tmp_path / "run",
            runner=runner,
            reference_area_m2=0.01,
            reference_length_m=0.1,
        )
    assert len(runner.calls) == 2
    message = str(caught.value)
    # It says what was actually tried, and does not declare a verdict it
    # cannot know: "a mesh or boundary-condition problem" sent the assistant
    # remeshing four times over a fault in the numerics.
    assert "first-order Roe" in message and "1.05x" in message
    assert "boundary-condition problem" not in message


def test_a_stage_without_a_restart_file_says_so(tmp_path, mesh_file):
    """Stage B with nothing to continue from must not restart from freestream."""
    runner = FakeRunner(
        scripted=[
            {"lines": diverging_rows()},
            {"lines": screen_rows(40, final_residual=-3.0)},  # no restart written
        ]
    )
    with pytest.raises(AeroSolverError, match="no restart file"):
        run_aero_case(
            request=make_request(),
            mesh_path=mesh_file,
            working_directory=tmp_path / "run",
            runner=runner,
            reference_area_m2=0.01,
            reference_length_m=0.1,
        )


# ---------------------------------------------------------------------------
# Regime-dependent numerics
# ---------------------------------------------------------------------------


def test_the_supersonic_config_starts_cautiously():
    """A cold supersonic start gets the gentle ramp that keeps it alive.

    Doubling the CFL every iteration from 5 puts a Mach 1.3 run past 150 by
    iteration six, which no 20-iteration linear solve can follow.
    """
    text = build_aero_config(
        FlowParams(velocity_value=1.3), SolverParams(), base_context()
    )
    settings = parse_config(text)

    assert float(settings["CFL_NUMBER"]) == pytest.approx(1.0)
    assert settings["CFL_ADAPT_PARAM"] == "( 0.5, 1.05, 0.1, 25 )"
    assert float(settings["ENTROPY_FIX_COEFF"]) == pytest.approx(0.10)
    assert float(settings["CFL_REDUCTION_TURB"]) == pytest.approx(0.5)
    assert settings["NUM_METHOD_GRAD"] == "GREEN_GAUSS"
    assert int(settings["LINEAR_SOLVER_ITER"]) == 20
    assert settings["WRT_RESTART_OVERWRITE"] == "YES"


def test_the_subsonic_config_no_longer_doubles_its_cfl():
    """Low subsonic keeps a brisk start, but not a ramp that doubles.

    A doubling ramp -- CFL 5 to 100 in five iterations -- is what sent a
    Mach 0.7 case to a NaN on every mesh it was given.
    """
    text = build_aero_config(
        FlowParams(velocity_value=0.3), SolverParams(), base_context()
    )
    settings = parse_config(text)

    assert float(settings["CFL_NUMBER"]) == pytest.approx(5.0)
    assert settings["CFL_ADAPT_PARAM"] == "( 0.5, 1.15, 0.1, 100 )"
    assert settings["NUM_METHOD_GRAD"] == "GREEN_GAUSS"
    assert "NUM_METHOD_GRAD_RECON" not in settings
    assert "CFL_REDUCTION_TURB" not in settings


def test_mach_0_7_starts_as_cautiously_as_a_transonic_case():
    """The case that failed: still JST, but started gently."""
    text = build_aero_config(
        FlowParams(velocity_value=0.7), SolverParams(), base_context()
    )
    settings = parse_config(text)

    assert settings["CONV_NUM_METHOD_FLOW"] == "JST"
    assert float(settings["CFL_NUMBER"]) == pytest.approx(2.0)
    assert settings["CFL_ADAPT_PARAM"] == "( 0.5, 1.1, 0.1, 50 )"
    assert float(settings["CFL_REDUCTION_TURB"]) == pytest.approx(0.5)
    assert settings["NUM_METHOD_GRAD"] == "GREEN_GAUSS"


def test_no_regime_ramps_by_more_than_a_fifth_per_iteration():
    """The mistake must not come back in some other band."""
    for mach in (0.2, 0.5, 0.6, 0.7, 0.79, 0.8, 1.0, 1.2, 2.0, 3.5):
        text = build_aero_config(
            FlowParams(velocity_value=mach), SolverParams(), base_context()
        )
        up = float(parse_config(text)["CFL_ADAPT_PARAM"].strip("( )").split(",")[1])
        assert up <= 1.2, f"Mach {mach} ramps x{up} per iteration"


def test_the_cfl_ramp_can_be_set_explicitly():
    """A low start means nothing if the ramp takes it straight back up."""
    text = build_aero_config(
        FlowParams(velocity_value=0.3),
        SolverParams(cfl_number=0.5, cfl_growth=1.02, cfl_max=4.0),
        base_context(),
    )
    settings = parse_config(text)
    assert float(settings["CFL_NUMBER"]) == pytest.approx(0.5)
    assert settings["CFL_ADAPT_PARAM"] == "( 0.5, 1.02, 0.1, 4 )"


def test_a_start_above_the_ceiling_lifts_the_ceiling():
    """An operator's explicit start is never clipped by a regime default."""
    text = build_aero_config(
        FlowParams(velocity_value=2.0), SolverParams(cfl_number=40.0), base_context()
    )
    assert parse_config(text)["CFL_ADAPT_PARAM"] == "( 0.5, 1.05, 0.1, 40 )"


def test_the_scheme_can_be_overridden():
    text = build_aero_config(
        FlowParams(velocity_value=0.7),
        SolverParams(convective_scheme=ConvectiveScheme.ROE),
        base_context(),
    )
    settings = parse_config(text)
    assert settings["CONV_NUM_METHOD_FLOW"] == "ROE"
    assert settings["MUSCL_FLOW"] == "YES"


def test_an_explicit_cfl_number_is_still_honoured():
    """Choosing a default must never override a number the operator typed."""
    text = build_aero_config(
        FlowParams(velocity_value=1.3),
        SolverParams(cfl_number=12.5),
        base_context(),
    )
    assert float(parse_config(text)["CFL_NUMBER"]) == pytest.approx(12.5)


def test_first_order_is_a_per_stage_override_not_a_new_default():
    """The rescue lowers the order for one stage; the default stays second."""
    from backend.aero_solver import replace

    solver = SolverParams()
    text = build_aero_config(
        FlowParams(velocity_value=1.3), replace(solver, muscl=False), base_context()
    )
    settings = parse_config(text)
    assert settings["MUSCL_FLOW"] == "NO"
    assert settings["CONV_NUM_METHOD_FLOW"] == "ROE"
    assert solver.muscl is True


def test_a_restart_needs_both_the_flag_and_the_filename():
    """Setting one without the other is a silent no-op."""
    from dataclasses import replace as dc_replace

    context = dc_replace(base_context(), restart_filename="solution_flow.dat")
    settings = parse_config(
        build_aero_config(
            FlowParams(velocity_value=1.3), SolverParams(restart=True), context
        )
    )
    assert settings["RESTART_SOL"] == "YES"
    assert settings["SOLUTION_FILENAME"] == "solution_flow.dat"


# ---------------------------------------------------------------------------
# The rocket frame: nose along +Z
# ---------------------------------------------------------------------------


def test_a_hinge_in_rocket_axes_gives_the_solver_frame_torque():
    """The same physical hinge, described in either frame, feels one torque."""
    from core.frames import Frame, to_rocket

    force = np.array([120.0, -8.0, 35.0])
    moment = np.array([0.4, -2.1, 0.7])
    origin = np.zeros(3)
    solver_hinge = HingeAxis(
        name="fin", point=[0.9, 0.05, 0.02], direction=[0.1, 1.0, 0.2]
    )
    rocket_hinge = HingeAxis(
        name="fin",
        point=to_rocket(solver_hinge.point),
        direction=to_rocket(solver_hinge.unit_direction()),
        frame=Frame.ROCKET,
    )

    from backend.aero_solver import hinge_torque

    in_solver = hinge_torque(moment, force, origin, solver_hinge)
    in_rocket = hinge_torque(moment, force, origin, rocket_hinge)
    assert in_rocket.torque_nm == pytest.approx(in_solver.torque_nm)
    # The moment vector comes back in the frame the hinge was given in.
    assert in_rocket.frame is Frame.ROCKET
    assert in_rocket.moment_vector_nm == pytest.approx(
        to_rocket(in_solver.moment_vector_nm)
    )


def test_the_result_carries_the_force_along_the_rocket_axes(mesh_file, tmp_path):
    """Drag on a rocket climbing nose-first is a negative Z force."""
    runner = FakeRunner(
        lines=screen_rows(120),
        artifacts={"forces_breakdown.dat": FORCES_BREAKDOWN},
    )
    result = run_aero_case(
        make_request(),
        mesh_path=mesh_file,
        working_directory=tmp_path / "run",
        runner=runner,
        reference_area_m2=0.005,
        reference_length_m=0.08,
    )
    fx, fy, fz = result.force_rocket_n
    assert fz == pytest.approx(-result.force_x_n)
    assert fy == pytest.approx(result.force_y_n)
    assert fx == pytest.approx(result.force_z_n)
    # Solver X runs nose to tail; the drag the air exerts points that way.
    assert result.force_x_n > 0.0 and fz < 0.0


def test_the_moment_origin_follows_a_rocket_frame_hinge(mesh_file, tmp_path):
    """SU2 is told the hinge point in its own frame, not the rocket's."""
    from core.frames import Frame

    runner = FakeRunner(
        lines=screen_rows(60), artifacts={"forces_breakdown.dat": FORCES_BREAKDOWN}
    )
    work = tmp_path / "run"
    run_aero_case(
        make_request(
            hinge_axes=[
                HingeAxis(
                    name="fin",
                    point=[0.0, 0.05, -0.9],
                    direction=[0, 1, 0],
                    frame=Frame.ROCKET,
                )
            ]
        ),
        mesh_path=mesh_file,
        working_directory=work,
        runner=runner,
        reference_area_m2=0.005,
        reference_length_m=0.08,
    )
    config = (work / "solver.cfg").read_text()
    line = next(row for row in config.splitlines() if row.startswith("REF_ORIGIN_MOMENT_X"))
    assert float(line.split("=")[1]) == pytest.approx(0.9)


def test_solver_warnings_are_reported_as_they_ended_not_as_they_began():
    """The y+ warning repeats every iteration; the last one is the answer."""
    from backend.aero_solver import _warning_notes
    from backend.su2_parser import SU2OutputParser

    parser = SU2OutputParser()
    for count in (12252, 4000, 900, 312):
        parser.feed(f"Warning: y+ < 5 in {count} points, for which the wall model is not active.")
    parser.feed("WARNING: SURFACE_PRESSURE_DROP can only be computed for at least 2 surfaces")
    notes = _warning_notes(parser)
    assert len(notes) == 2
    assert any("312 points" in note for note in notes)
    assert not any("12252" in note for note in notes)


def test_a_low_mach_residual_that_starts_low_is_not_convergence():
    """Mach 0.1: RMS[Rho] starts near -4.8 and crossed -5 at iteration 15.

    The absolute threshold stopped the solve there and called it converged,
    with C_d still swinging wildly. What counts is the drop from the peak.
    """
    from backend.su2_parser import relative_residual_drop

    lines = ["+" + "-" * 70 + "+", SCREEN_HEADER, "+" + "-" * 70 + "+"]
    for index, (residual, cd) in enumerate(
        [(-4.35, 40.9), (-4.64, 45.9), (-4.71, 42.1), (-4.52, 28.8), (-4.62, 12.9)]
        + [(-4.9 - 0.02 * i, 4.0 - 0.1 * i) for i in range(10)]
    ):
        lines.append(
            "|{:12d}|{:12.6f}|{:12.6f}|{:12.6f}|{:12.6f}|{:12.6f}"
            "|{:12.6f}|{:12.6f}|{:12.6f}|".format(
                index, residual, residual - 1.0, 0.0, cd, 0.0, 0.0, 0.0, 0.0
            )
        )
    parser = SU2OutputParser()
    for line in lines:
        parser.feed(line)
    assert parser.final_value("rms_rho") < -5.0  # the old trap
    assert relative_residual_drop(parser) > -1.0
    monitor = ConvergenceMonitor(residual_threshold=-5.0, minimum_iterations=10)
    assert not monitor.should_stop(parser)


def test_the_config_converges_on_the_relative_residual():
    settings = parse_config(
        build_aero_config(FlowParams(velocity_value=0.5), SolverParams(), base_context())
    )
    assert settings["CONV_FIELD"] == "REL_RMS_DENSITY"
    assert int(settings["CONV_STARTITER"]) >= 100


def test_the_centre_of_pressure_is_undefined_at_zero_incidence(mesh_file, tmp_path):
    """A sweep reported it 5.9 m ahead of the nose, from noise over noise."""
    runner = FakeRunner(
        lines=screen_rows(120), artifacts={"forces_breakdown.dat": FORCES_BREAKDOWN}
    )
    result = run_aero_case(
        make_request(flow=FlowParams(velocity_value=1.3, aoa_deg=0.0)),
        mesh_path=mesh_file,
        working_directory=tmp_path / "run",
        runner=runner,
        reference_area_m2=0.005,
        reference_length_m=0.08,
    )
    assert math.isnan(result.center_of_pressure_rocket[2])


def test_a_solver_that_hangs_before_its_first_iteration_is_started_again(
    mesh_file, tmp_path
):
    """An MPI start-up hang is retried once; a result follows."""
    from backend.runner import SolverTimeoutError

    class HangsOnce(FakeRunner):
        def run(self, *args, **kwargs):
            if not getattr(self, "_hung", False):
                self._hung = True
                self.calls.append({"config_text": "hung"})
                raise SolverTimeoutError("stall", 300.0, 300.0, ["Reading mesh ..."])
            return super().run(*args, **kwargs)

    runner = HangsOnce(
        lines=screen_rows(120), artifacts={"forces_breakdown.dat": FORCES_BREAKDOWN}
    )
    result = run_aero_case(
        make_request(),
        mesh_path=mesh_file,
        working_directory=tmp_path / "run",
        runner=runner,
        reference_area_m2=0.005,
        reference_length_m=0.08,
    )
    assert result.cd == pytest.approx(0.42)
    assert "restarted automatically" in result.notes[0]


def test_a_solver_that_stalls_mid_run_is_not_retried(mesh_file, tmp_path):
    from backend.runner import SolverTimeoutError

    tail = screen_rows(5)

    class StallsMidRun(FakeRunner):
        def run(self, *args, **kwargs):
            self.calls.append({})
            raise SolverTimeoutError("stall", 300.0, 900.0, tail)

    runner = StallsMidRun(lines=[])
    with pytest.raises(SolverTimeoutError):
        run_aero_case(
            make_request(),
            mesh_path=mesh_file,
            working_directory=tmp_path / "run",
            runner=runner,
            reference_area_m2=0.005,
            reference_length_m=0.08,
        )
    assert len(runner.calls) == 1


def test_a_low_mach_result_says_how_it_was_solved(mesh_file, tmp_path):
    runner = FakeRunner(lines=screen_rows(120), artifacts={"forces_breakdown.dat": FORCES_BREAKDOWN})
    result = run_aero_case(
        make_request(flow=FlowParams(velocity_value=0.1)),
        mesh_path=mesh_file, working_directory=tmp_path / "run", runner=runner,
        reference_area_m2=0.005, reference_length_m=0.08,
    )
    assert any("incompressible" in note for note in result.notes)


# ---------------------------------------------------------------------------
# Low Mach: the incompressible solver
# ---------------------------------------------------------------------------


def test_low_mach_is_solved_incompressible():
    """Below Mach 0.3 the compressible solver's dissipation swamps the physics.

    On the Sapphire at Mach 0.1 it gave C_d 2.2 where the second-order
    incompressible solution is about 1.3.
    """
    settings = parse_config(
        build_aero_config(
            FlowParams(velocity_value=0.1, aoa_deg=7.0), SolverParams(), base_context()
        )
    )
    assert settings["SOLVER"] == "INC_RANS"
    assert settings["CONV_NUM_METHOD_FLOW"] == "FDS"
    assert settings["MUSCL_FLOW"] == "YES"
    assert settings["CONV_FIELD"] == "REL_RMS_PRESSURE"
    assert "FLUID_MODEL" not in settings and "RMS_DENSITY" not in settings["SCREEN_OUTPUT"]
    # The velocity carries the angle; AOA stays for projecting lift and drag.
    u, v, w = (float(x) for x in settings["INC_VELOCITY_INIT"].strip("() ").split(","))
    assert math.degrees(math.atan2(w, u)) == pytest.approx(7.0)
    assert v == pytest.approx(0.0, abs=1e-9)
    assert math.hypot(u, w) == pytest.approx(0.1 * 340.29, rel=1e-3)
    assert float(settings["AOA"]) == pytest.approx(7.0)


def test_a_named_scheme_keeps_low_mach_compressible():
    settings = parse_config(
        build_aero_config(
            FlowParams(velocity_value=0.1),
            SolverParams(convective_scheme="ROE"),
            base_context(),
        )
    )
    assert settings["SOLVER"] == "RANS"


def test_mach_point_three_and_up_stays_compressible():
    settings = parse_config(
        build_aero_config(FlowParams(velocity_value=0.3), SolverParams(), base_context())
    )
    assert settings["SOLVER"] == "RANS"


def test_the_incompressible_pressure_residual_drives_convergence():
    """No density equation, so rms[P] plays the part rms[Rho] usually does."""
    parser = SU2OutputParser()
    parser.feed("|  Inner_Iter|      rms[P]|      rms[U]|          CL|          CD|")
    record = parser.feed("|          12|   -4.244849|  -5.635413|    0.480182|    1.723264|")
    assert record is not None
    assert record.rms_rho == pytest.approx(-4.244849)
    assert record.get("cd") == pytest.approx(1.723264)
