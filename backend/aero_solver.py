"""Aerodynamic simulation: run SU2 and reduce its output to engineering data.

The solver itself is only half the job. What the operator needs is dimensional
forces, the centre of pressure, and the torque a servo must hold against at
each fin hinge. Those reductions are implemented here, with the geometry done
explicitly rather than leaning on SU2's own moment output, because the hinge
axis is an arbitrary line that SU2 knows nothing about.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from backend.runner import RunOutcome, SolverRunner
from backend.su2_config import build_config_for_request, write_config
from backend.su2_parser import (
    ConvergenceMonitor,
    SU2OutputParser,
    parse_forces_breakdown,
)
from core.models import (
    AeroResult,
    AeroRunRequest,
    HingeAxis,
    HingeTorqueResult,
    ReferenceValues,
)
from core.units import dynamic_pressure

# Name of the config file written into each run directory.
CONFIG_FILENAME = "solver.cfg"
FORCES_BREAKDOWN_FILENAME = "forces_breakdown.dat"


class AeroSolverError(RuntimeError):
    """Raised when an aerodynamic run cannot be completed or interpreted."""


# ---------------------------------------------------------------------------
# Geometric reductions
# ---------------------------------------------------------------------------


def transfer_moment(
    moment_about_origin: np.ndarray,
    force: np.ndarray,
    origin: np.ndarray,
    new_point: np.ndarray,
) -> np.ndarray:
    """Transfer a moment to a different reference point.

    ``M_new = M_old + (r_old - r_new) x F``

    SU2 reports moments about the configured reference origin. A fin hinge is
    somewhere else entirely, and the torque a servo feels is the moment about
    *that* line, so the parallel-axis transfer is not optional.

    Parameters
    ----------
    moment_about_origin:
        Moment vector about ``origin``, N m.
    force:
        Total force vector, N.
    origin:
        Point the moment is currently referenced to.
    new_point:
        Point to transfer the moment to.

    Returns
    -------
    ndarray
        Moment vector about ``new_point``, N m.
    """
    offset = np.asarray(origin, dtype=float) - np.asarray(new_point, dtype=float)
    return np.asarray(moment_about_origin, dtype=float) + np.cross(
        offset, np.asarray(force, dtype=float)
    )


def hinge_torque(
    moment_about_origin: np.ndarray,
    force: np.ndarray,
    moment_origin: np.ndarray,
    hinge: HingeAxis,
) -> HingeTorqueResult:
    """Scalar servo torque about a fin hinge axis.

    The moment is transferred to the hinge point and then projected onto the
    hinge direction. Only the component along the axis can drive rotation; the
    perpendicular components are reacted by the bearing.

    Parameters
    ----------
    moment_about_origin:
        Aerodynamic moment about ``moment_origin``, N m.
    force:
        Total aerodynamic force, N.
    moment_origin:
        Point the supplied moment refers to.
    hinge:
        The hinge axis, giving a point and a direction.

    Returns
    -------
    HingeTorqueResult
        The scalar torque and the full moment vector about the hinge point.
    """
    point = np.asarray(hinge.point, dtype=float)
    direction = np.asarray(hinge.unit_direction(), dtype=float)
    moment_at_hinge = transfer_moment(
        moment_about_origin, force, moment_origin, point
    )
    return HingeTorqueResult(
        name=hinge.name,
        torque_nm=float(np.dot(moment_at_hinge, direction)),
        moment_vector_nm=[float(v) for v in moment_at_hinge],
    )


def center_of_pressure(
    force: np.ndarray,
    moment_about_origin: np.ndarray,
    moment_origin: np.ndarray,
    axis: int = 0,
) -> np.ndarray:
    """Locate the centre of pressure along the body axis.

    The centre of pressure is where the resultant force produces no moment.
    That condition defines a line rather than a point, so the conventional
    choice is taken: the point on the body axis (``y = z = 0`` for a rocket)
    at the axial station where the transverse moments vanish.

    For a body aligned with +X, the station follows from the pitching moment
    and the normal force as ``x_cp = x_ref - M_y / F_z``.

    Parameters
    ----------
    force:
        Total aerodynamic force vector, N.
    moment_about_origin:
        Moment about ``moment_origin``, N m.
    moment_origin:
        Reference point of the moment.
    axis:
        Index of the body axis (0 for +X).

    Returns
    -------
    ndarray
        Centre-of-pressure coordinates. Non-finite where the transverse force
        is too small to define one, which is the honest answer at zero
        incidence on a symmetric body.
    """
    force = np.asarray(force, dtype=float)
    moment = np.asarray(moment_about_origin, dtype=float)
    origin = np.asarray(moment_origin, dtype=float)

    result = origin.astype(float).copy()
    # Transverse axes: the two that are not the body axis.
    transverse = [index for index in range(3) if index != axis]
    first, second = transverse

    normal_force = math.hypot(force[first], force[second])
    if normal_force < 1.0e-12:
        result[axis] = math.nan
        return result

    # Use whichever transverse component is better conditioned.
    if abs(force[second]) >= abs(force[first]):
        # x_cp = x_ref - M_y / F_z for axis=0, first=1 (y), second=2 (z).
        shift = -moment[first] / force[second]
    else:
        shift = moment[second] / force[first]

    result[axis] = origin[axis] + shift
    result[first] = 0.0
    result[second] = 0.0
    return result


@dataclass
class ForceSet:
    """Dimensional forces and moments recovered from coefficients."""

    force: np.ndarray
    moment: np.ndarray
    drag_n: float
    lift_n: float
    sideforce_n: float

    @property
    def magnitude_n(self) -> float:
        """Resultant force magnitude, N."""
        return float(np.linalg.norm(self.force))


def dimensionalise(
    coefficients: dict[str, float],
    dynamic_pressure_pa: float,
    reference_area_m2: float,
    reference_length_m: float,
) -> ForceSet:
    """Convert SU2 coefficients into dimensional forces and moments.

    Force coefficients scale by ``q * S``; moment coefficients additionally by
    the reference length.
    """
    scale = dynamic_pressure_pa * reference_area_m2
    moment_scale = scale * reference_length_m

    force = np.array(
        [
            coefficients.get("cfx", 0.0) * scale,
            coefficients.get("cfy", 0.0) * scale,
            coefficients.get("cfz", 0.0) * scale,
        ]
    )
    moment = np.array(
        [
            coefficients.get("cmx", 0.0) * moment_scale,
            coefficients.get("cmy", 0.0) * moment_scale,
            coefficients.get("cmz", 0.0) * moment_scale,
        ]
    )
    return ForceSet(
        force=force,
        moment=moment,
        drag_n=coefficients.get("cd", 0.0) * scale,
        lift_n=coefficients.get("cl", 0.0) * scale,
        sideforce_n=coefficients.get("cs", 0.0) * scale,
    )


# ---------------------------------------------------------------------------
# Running a case
# ---------------------------------------------------------------------------


def run_aero_case(
    request: AeroRunRequest,
    mesh_path: Path | str,
    working_directory: Path | str,
    runner: SolverRunner,
    reference_area_m2: float,
    reference_length_m: float,
    sim_id: str | None = None,
    on_iteration: callable | None = None,
    on_line: callable | None = None,
) -> AeroResult:
    """Run one aerodynamic simulation point and reduce the results.

    Parameters
    ----------
    request:
        Flow conditions, solver settings and hinge axes.
    mesh_path:
        Path to the ``.su2`` mesh. Copied into the working directory if it is
        not already there, since SU2 resolves the mesh relative to its own
        working directory.
    working_directory:
        Directory the solver runs in and writes its outputs to.
    runner:
        Solver backend. Inject a ``FakeRunner`` to test without SU2.
    reference_area_m2, reference_length_m:
        Measured reference quantities, used unless overridden in the request.
    sim_id:
        Identifier recorded in the result.
    on_iteration:
        Called with each parsed :class:`IterationRecord`, for live plotting.
    on_line:
        Called with each raw output line.

    Returns
    -------
    AeroResult
        Forces, coefficients, centre of pressure and hinge torques.
    """
    mesh_path = Path(mesh_path)
    working_directory = Path(working_directory)
    working_directory.mkdir(parents=True, exist_ok=True)

    if not mesh_path.is_file():
        raise AeroSolverError(f"mesh file not found: {mesh_path}")

    local_mesh = working_directory / mesh_path.name
    if mesh_path.resolve() != local_mesh.resolve():
        local_mesh.write_bytes(mesh_path.read_bytes())

    config_text = build_config_for_request(
        request,
        mesh_filename=local_mesh.name,
        reference=request.reference,
        measured_area_m2=reference_area_m2,
        measured_length_m=reference_length_m,
    )
    config_path = write_config(config_text, working_directory / CONFIG_FILENAME)

    parser = SU2OutputParser()
    monitor = ConvergenceMonitor(
        residual_threshold=request.solver.convergence_residual,
        force_window=request.solver.force_stabilization_window,
        force_tolerance=request.solver.force_stabilization_tol,
    )

    def handle_line(line: str) -> None:
        record = parser.feed(line)
        if on_line is not None:
            on_line(line)
        if record is not None and on_iteration is not None:
            on_iteration(record)

    def should_stop(_: str) -> bool:
        return monitor.should_stop(parser)

    started = time.perf_counter()
    outcome: RunOutcome = runner.run(
        config_path=config_path,
        working_directory=working_directory,
        ranks=request.solver.mpi_ranks,
        on_line=handle_line,
        should_stop=should_stop,
    )
    wall_time = time.perf_counter() - started

    if parser.errors:
        raise AeroSolverError(
            "SU2 reported an error:\n" + "\n".join(parser.errors[:5])
        )
    if not parser.records:
        raise AeroSolverError(
            "no iteration history was parsed from the solver output; the run "
            "probably failed to start.\n" + "\n".join(outcome.tail(15))
        )

    coefficients = _collect_coefficients(parser, working_directory)
    return _build_result(
        request=request,
        sim_id=sim_id or working_directory.name,
        mesh_path=mesh_path,
        coefficients=coefficients,
        parser=parser,
        monitor=monitor,
        reference_area_m2=(
            request.reference.reference_area_m2 or reference_area_m2
        ),
        reference_length_m=(
            request.reference.reference_length_m or reference_length_m
        ),
        wall_time_s=wall_time,
        stopped_early=outcome.stopped_early,
    )


def _collect_coefficients(
    parser: SU2OutputParser, working_directory: Path
) -> dict[str, float]:
    """Prefer the forces breakdown file, falling back to screen output.

    The breakdown file carries the full coefficient set including the body
    force components; the screen table may omit some of them depending on
    what was requested.
    """
    coefficients: dict[str, float] = {}
    breakdown_path = working_directory / FORCES_BREAKDOWN_FILENAME
    if breakdown_path.is_file():
        try:
            coefficients.update(parse_forces_breakdown(breakdown_path))
        except (OSError, ValueError):  # pragma: no cover - malformed file
            pass

    for name in ("cd", "cl", "cs", "cmx", "cmy", "cmz", "cfx", "cfy", "cfz"):
        if name not in coefficients or not math.isfinite(coefficients[name]):
            value = parser.final_value(name)
            if math.isfinite(value):
                coefficients[name] = value

    if "cd" not in coefficients:
        raise AeroSolverError(
            "no drag coefficient found in either forces_breakdown.dat or the "
            "solver screen output"
        )
    return coefficients


def _build_result(
    request: AeroRunRequest,
    sim_id: str,
    mesh_path: Path,
    coefficients: dict[str, float],
    parser: SU2OutputParser,
    monitor: ConvergenceMonitor,
    reference_area_m2: float,
    reference_length_m: float,
    wall_time_s: float,
    stopped_early: bool,
) -> AeroResult:
    """Assemble the final result from parsed coefficients."""
    flow = request.flow
    state = flow.atmosphere()
    speed = flow.speed_ms()
    q = dynamic_pressure(state.density_kg_m3, speed)

    forces = dimensionalise(
        coefficients, q, reference_area_m2, reference_length_m
    )

    # Body-axis force components are not always reported; recover them from
    # the wind-axis coefficients when missing.
    if "cfx" not in coefficients:
        forces.force = _body_forces_from_wind_axes(
            forces.drag_n, forces.lift_n, forces.sideforce_n, flow
        )

    moment_origin = _resolve_moment_origin(request)
    torques = [
        hinge_torque(forces.moment, forces.force, moment_origin, hinge)
        for hinge in request.hinge_axes
    ]

    cop = center_of_pressure(forces.force, forces.moment, moment_origin)
    residual = parser.final_value("rms_rho")

    return AeroResult(
        sim_id=sim_id,
        mesh_id=mesh_path.stem,
        mach=flow.mach(),
        speed_ms=speed,
        aoa_deg=flow.aoa_deg,
        sideslip_deg=flow.sideslip_deg,
        dynamic_pressure_pa=q,
        reference_area_m2=reference_area_m2,
        reference_length_m=reference_length_m,
        force_x_n=float(forces.force[0]),
        force_y_n=float(forces.force[1]),
        force_z_n=float(forces.force[2]),
        drag_n=forces.drag_n,
        lift_n=forces.lift_n,
        sideforce_n=forces.sideforce_n,
        cd=coefficients.get("cd", math.nan),
        cl=coefficients.get("cl", math.nan),
        cs=coefficients.get("cs", 0.0),
        cm_pitch=coefficients.get("cmy", 0.0),
        center_of_pressure=[float(v) for v in cop],
        hinge_torques=torques,
        iterations=parser.records[-1].iteration if parser.records else 0,
        final_residual_rho=residual,
        converged=bool(
            stopped_early
            or (math.isfinite(residual) and residual <= request.solver.convergence_residual)
        ),
        wall_time_s=wall_time_s,
    )


def _resolve_moment_origin(request: AeroRunRequest) -> np.ndarray:
    """The point SU2 referenced its moments to, mirroring the config writer."""
    if request.reference.moment_origin is not None:
        return np.array(request.reference.moment_origin, dtype=float)
    if request.hinge_axes:
        return np.array(request.hinge_axes[0].point, dtype=float)
    return np.zeros(3)


def _body_forces_from_wind_axes(
    drag_n: float, lift_n: float, sideforce_n: float, flow
) -> np.ndarray:
    """Rotate wind-axis forces into body axes.

    SU2 defines drag along the freestream direction, which is inclined from
    the body axis by the angle of attack and sideslip.
    """
    alpha = math.radians(flow.aoa_deg)
    beta = math.radians(flow.sideslip_deg)

    cos_a, sin_a = math.cos(alpha), math.sin(alpha)
    cos_b, sin_b = math.cos(beta), math.sin(beta)

    # Wind-to-body rotation for the standard aerospace convention.
    x = -drag_n * cos_a * cos_b - sideforce_n * cos_a * sin_b + lift_n * sin_a
    y = -drag_n * sin_b + sideforce_n * cos_b
    z = -drag_n * sin_a * cos_b - sideforce_n * sin_a * sin_b - lift_n * cos_a
    return np.array([x, y, z])
