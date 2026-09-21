"""Parametric sweeps across flow conditions."""

from __future__ import annotations

import math

import pytest

from backend.runner import FakeRunner
from backend.sweep import (
    SweepError,
    linear_values,
    resolve_parameter,
    run_parametric_sweep,
)
from core.models import AeroRunRequest, FlowParams, HingeAxis, SolverParams

SCREEN_HEADER = (
    "|  Inner_Iter|    rms[Rho]|          CL|          CD|        CMy|"
)


def screen_rows(count: int, cd: float = 0.42, cl: float = 0.25) -> list[str]:
    """Converging solver output with fixed coefficients."""
    lines = ["+---+", SCREEN_HEADER, "+---+"]
    for index in range(count):
        residual = -1.0 - 5.0 * (index + 1) / count
        lines.append(
            "|{:12d}|{:12.6f}|{:12.6f}|{:12.6f}|{:12.6f}|".format(
                index, residual, cl, cd, -0.05
            )
        )
    return lines


@pytest.fixture
def mesh_file(tmp_path):
    """A stand-in mesh shared by every sweep point."""
    path = tmp_path / "mesh.su2"
    path.write_text("NDIME= 3\n")
    return path


def base_request(**overrides) -> AeroRunRequest:
    """A template request for a sweep."""
    return AeroRunRequest(
        mesh_id="mesh-1",
        flow=FlowParams(velocity_value=2.0, aoa_deg=2.0),
        solver=SolverParams(mpi_ranks=1, force_stabilization_window=10),
        hinge_axes=overrides.pop("hinge_axes", []),
    )


def run(mesh_file, tmp_path, runner=None, **kwargs):
    """Run a sweep with sensible defaults."""
    return run_parametric_sweep(
        kwargs.pop("base", base_request()),
        parameter=kwargs.pop("parameter", "mach"),
        values=kwargs.pop("values", [0.5, 1.5, 2.5]),
        mesh_path=mesh_file,
        output_root=tmp_path / "sweep",
        runner=runner or FakeRunner(lines=screen_rows(40)),
        reference_area_m2=0.005,
        reference_length_m=0.08,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Parameter resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("mach", "velocity_value"),
        ("Mach", "velocity_value"),
        ("aoa", "aoa_deg"),
        ("alpha", "aoa_deg"),
        ("beta", "sideslip_deg"),
        ("altitude", "altitude_m"),
    ],
)
def test_parameter_aliases_resolve(name, expected):
    """Common spellings map onto the underlying field."""
    assert resolve_parameter(name) == expected


def test_unknown_parameter_lists_the_valid_ones():
    """The error tells the caller what they can sweep."""
    with pytest.raises(SweepError, match="not a sweepable parameter"):
        resolve_parameter("wing_area")


def test_linear_values_is_inclusive():
    """Endpoints are included, as an operator expects."""
    assert linear_values(0.0, 1.0, 3) == [0.0, 0.5, 1.0]
    assert linear_values(2.0, 2.0, 1) == [2.0]
    with pytest.raises(SweepError):
        linear_values(0.0, 1.0, 0)


# ---------------------------------------------------------------------------
# Running sweeps
# ---------------------------------------------------------------------------


def test_sweep_runs_one_case_per_value(mesh_file, tmp_path):
    """Every requested value produces a point."""
    sweep = run(mesh_file, tmp_path, values=[0.5, 1.0, 2.0, 3.0])
    assert len(sweep.points) == 4
    assert len(sweep.succeeded_points) == 4
    assert [point.value for point in sweep.points] == [0.5, 1.0, 2.0, 3.0]


def test_sweep_varies_only_the_swept_parameter(mesh_file, tmp_path):
    """Fixed conditions are held across every point."""
    sweep = run(mesh_file, tmp_path, parameter="mach", values=[0.5, 2.5])
    for point in sweep.succeeded_points:
        assert point.result is not None
        assert point.result.aoa_deg == 2.0  # from the base request
    assert [p.result.mach for p in sweep.succeeded_points] == [0.5, 2.5]


def test_sweeping_angle_of_attack(mesh_file, tmp_path):
    """An alpha sweep produces a polar."""
    sweep = run(mesh_file, tmp_path, parameter="aoa", values=[-5.0, 0.0, 5.0])
    assert [p.result.aoa_deg for p in sweep.succeeded_points] == [-5.0, 0.0, 5.0]
    # Mach was held fixed.
    assert all(p.result.mach == 2.0 for p in sweep.succeeded_points)


def test_sweep_reuses_one_mesh(mesh_file, tmp_path):
    """Every point solves on the same mesh, which is the point of a sweep."""
    runner = FakeRunner(lines=screen_rows(40))
    sweep = run(mesh_file, tmp_path, runner=runner, values=[1.0, 2.0])
    assert len(runner.calls) == 2
    assert len(sweep.succeeded_points) == 2


def test_each_point_gets_its_own_directory(mesh_file, tmp_path):
    """Outputs must not overwrite one another."""
    run(mesh_file, tmp_path, values=[1.0, 2.0, 3.0])
    directories = sorted((tmp_path / "sweep").glob("point_*"))
    assert len(directories) == 3


def test_a_failing_point_does_not_discard_the_sweep(mesh_file, tmp_path):
    """One bad condition must not throw away an expensive batch."""
    sweep = run(
        mesh_file,
        tmp_path,
        values=[1.0, 99.0, 2.0],  # 99 is outside the Mach envelope
    )
    assert len(sweep.points) == 3
    assert len(sweep.succeeded_points) == 2
    assert len(sweep.failed_points) == 1
    assert sweep.failed_points[0].value == 99.0
    assert sweep.failed_points[0].error


def test_stop_on_error_aborts_the_sweep(mesh_file, tmp_path):
    """Opting in stops at the first failure."""
    sweep = run(
        mesh_file, tmp_path, values=[99.0, 1.0, 2.0], stop_on_error=True
    )
    assert len(sweep.points) == 1
    assert not sweep.points[0].succeeded


def test_progress_callback_fires_per_point(mesh_file, tmp_path):
    """The GUI needs progress as the batch proceeds."""
    seen = []
    run(mesh_file, tmp_path, values=[1.0, 2.0], on_point=seen.append)
    assert len(seen) == 2


def test_empty_value_list_is_rejected(mesh_file, tmp_path):
    """An empty sweep is a mistake."""
    with pytest.raises(SweepError, match="at least one value"):
        run(mesh_file, tmp_path, values=[])


def test_missing_mesh_is_reported(tmp_path):
    """A bad mesh path fails before any solve starts."""
    with pytest.raises(SweepError, match="mesh file not found"):
        run_parametric_sweep(
            base_request(),
            parameter="mach",
            values=[1.0],
            mesh_path=tmp_path / "absent.su2",
            output_root=tmp_path / "sweep",
            runner=FakeRunner(lines=[]),
            reference_area_m2=0.005,
            reference_length_m=0.08,
        )


# ---------------------------------------------------------------------------
# Curves and extremes
# ---------------------------------------------------------------------------


def test_curve_extraction_pairs_values_with_results(mesh_file, tmp_path):
    """A curve is the swept value against a coefficient."""
    sweep = run(mesh_file, tmp_path, values=[0.5, 1.5, 2.5])
    xs, ys = sweep.curve("cd")
    assert xs == [0.5, 1.5, 2.5]
    assert all(y == pytest.approx(0.42) for y in ys)


def test_curve_omits_failed_points(mesh_file, tmp_path):
    """Failed conditions leave gaps rather than corrupt the curve."""
    sweep = run(mesh_file, tmp_path, values=[1.0, 99.0, 2.0])
    xs, _ = sweep.curve("cd")
    assert xs == [1.0, 2.0]


def test_summary_curves_cover_the_standard_quantities(mesh_file, tmp_path):
    """The MCP response carries every curve an operator plots."""
    sweep = run(mesh_file, tmp_path, values=[1.0, 2.0])
    curves = sweep.summary_curves()
    assert set(curves) >= {"values", "cd", "cl", "drag_n", "lift_n"}
    assert len(curves["cd"]) == len(curves["values"]) == 2


def test_hinge_torque_curves_are_included(mesh_file, tmp_path):
    """Torque against Mach is the curve a servo is chosen from."""
    sweep = run(
        mesh_file,
        tmp_path,
        base=base_request(
            hinge_axes=[
                HingeAxis(name="fin_a", point=[0.9, 0.05, 0.0], direction=[0, 1, 0])
            ]
        ),
        values=[1.0, 2.0],
    )
    curves = sweep.summary_curves()
    assert "torque_fin_a_nm" in curves
    assert len(curves["torque_fin_a_nm"]) == 2

    xs, ys = sweep.curve("torque:fin_a")
    assert len(xs) == len(ys) == 2


def test_extremes_report_the_servo_sizing_torque(mesh_file, tmp_path):
    """The worst-case hinge torque across the sweep is surfaced."""
    sweep = run(
        mesh_file,
        tmp_path,
        base=base_request(
            hinge_axes=[
                HingeAxis(name="fin_a", point=[0.9, 0.05, 0.0], direction=[0, 1, 0])
            ]
        ),
        values=[1.0, 2.0, 3.0],
    )
    extremes = sweep.extremes()
    assert "servo_sizing_torque_nm" in extremes
    assert extremes["servo_sizing_torque_nm"] == pytest.approx(
        max(extremes["max_abs_hinge_torque_nm"].values())
    )
    # Drag grows with dynamic pressure, so the fastest point is the worst.
    assert extremes["max_drag_at"] == pytest.approx(3.0)


def test_extremes_of_an_all_failed_sweep_are_empty(mesh_file, tmp_path):
    """No results means no extremes, rather than a crash."""
    sweep = run(mesh_file, tmp_path, values=[99.0, 98.0])
    assert sweep.extremes() == {}


def test_sweep_report_is_json_serialisable(mesh_file, tmp_path):
    """The whole report crosses the MCP wire."""
    import json

    sweep = run(mesh_file, tmp_path, values=[1.0, 2.0])
    payload = sweep.as_dict()
    json.dumps(payload)
    assert payload["succeeded"] == 2
    assert payload["parameter"] == "mach"
    assert payload["wall_time_s"] >= 0.0
