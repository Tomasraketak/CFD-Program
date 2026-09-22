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
    SolverParams,
)
from core.units import dynamic_pressure

# Name of the config file written into each run directory. The rescue stages
# get their own names rather than overwriting this, so a post-mortem shows
# what was actually run at each attempt.
CONFIG_FILENAME = "solver.cfg"
RESCUE_A_CONFIG_FILENAME = "solver_stage_a.cfg"
RESCUE_B_CONFIG_FILENAME = "solver_stage_b.cfg"
FORCES_BREAKDOWN_FILENAME = "forces_breakdown.dat"

# SU2 writes its restart here; stage B reads a copy under a distinct name.
# Aliasing the two usually works serially and is one MPI-IO ordering change
# away from reading a file that was just truncated.
RESTART_FILENAME = "restart_flow.dat"
SOLUTION_FILENAME = "solution_flow.dat"

# Marker emitted on the line stream when a rescue stage begins, so the live
# residual plot can start a new series instead of drawing a sawtooth when the
# iteration counter restarts at 1.
STAGE_MARKER_PREFIX = "### AeroThermalStudio stage:"


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

    solver = request.solver
    started = time.perf_counter()
    notes: list[str] = []

    def run_stage(
        stage_solver,
        config_name: str,
        allow_early_stop: bool,
        history_filename: str = "history",
        restart_from: str | None = None,
        write_frequency: int = 250,
    ) -> _StageOutcome:
        return _run_stage(
            request=request,
            solver=stage_solver,
            mesh_filename=local_mesh.name,
            reference_area_m2=reference_area_m2,
            reference_length_m=reference_length_m,
            working_directory=working_directory,
            runner=runner,
            config_name=config_name,
            allow_early_stop=allow_early_stop,
            history_filename=history_filename,
            restart_from=restart_from,
            write_frequency=write_frequency,
            on_line=on_line,
            on_iteration=on_iteration,
        )

    stage = run_stage(solver, CONFIG_FILENAME, allow_early_stop=True)

    if stage.diverged:
        if not solver.rescue_on_divergence:
            raise AeroSolverError(
                f"the solve diverged ({stage.divergence_reason}). Automatic "
                "rescue is disabled; lower the CFL number, turn off MUSCL "
                "reconstruction, or refine the mesh."
            )
        stage, notes = _rescue(
            request=request,
            first_attempt=stage,
            working_directory=working_directory,
            run_stage=run_stage,
            on_line=on_line,
        )

    wall_time = time.perf_counter() - started
    parser = stage.parser

    if parser.errors:
        raise AeroSolverError(
            "SU2 reported an error:\n" + "\n".join(parser.errors[:5])
        )
    if not parser.records:
        raise AeroSolverError(
            "no iteration history was parsed from the solver output; the run "
            "probably failed to start.\n" + "\n".join(stage.outcome.tail(15))
        )

    notes.extend(_warning_notes(parser))

    coefficients = _collect_coefficients(parser, working_directory)
    result = _build_result(
        request=request,
        sim_id=sim_id or working_directory.name,
        mesh_path=mesh_path,
        coefficients=coefficients,
        parser=parser,
        monitor=stage.monitor,
        reference_area_m2=(
            request.reference.reference_area_m2 or reference_area_m2
        ),
        reference_length_m=(
            request.reference.reference_length_m or reference_length_m
        ),
        wall_time_s=wall_time,
        stopped_early=stage.outcome.stopped_early,
    )
    return result.model_copy(update={"rescued": stage.rescued, "notes": notes})


@dataclass
class _StageOutcome:
    """What one solver invocation produced."""

    parser: SU2OutputParser
    monitor: ConvergenceMonitor
    outcome: RunOutcome
    diverged: bool
    divergence_reason: str = ""
    rescued: bool = False


def _run_stage(
    request: AeroRunRequest,
    solver,
    mesh_filename: str,
    reference_area_m2: float,
    reference_length_m: float,
    working_directory: Path,
    runner: SolverRunner,
    config_name: str,
    allow_early_stop: bool,
    history_filename: str,
    restart_from: str | None,
    write_frequency: int,
    on_line: callable | None,
    on_iteration: callable | None,
) -> _StageOutcome:
    """Write a config, run it, and parse the stream it produces.

    Each stage gets a fresh parser and monitor. Sharing them would leave a
    previous stage's blown-up rows in the history, so divergence would re-fire
    immediately and the reported iteration count would belong to the wrong
    attempt.
    """
    config_text = build_config_for_request(
        request,
        mesh_filename=mesh_filename,
        reference=request.reference,
        measured_area_m2=reference_area_m2,
        measured_length_m=reference_length_m,
        solver=solver,
        restart_filename=restart_from,
        history_filename=history_filename,
        output_write_frequency=write_frequency,
    )
    config_path = write_config(config_text, working_directory / config_name)

    parser = SU2OutputParser()
    monitor = ConvergenceMonitor(
        residual_threshold=solver.convergence_residual,
        force_window=solver.force_stabilization_window,
        force_tolerance=solver.force_stabilization_tol,
    )
    state = {"diverged": False, "reason": ""}

    def handle_line(line: str) -> None:
        record = parser.feed(line)
        if on_line is not None:
            on_line(line)
        if record is not None and on_iteration is not None:
            on_iteration(record)

    def should_stop(_: str) -> bool:
        if monitor.diverged(parser):
            state["diverged"] = True
            state["reason"] = monitor.reason
            # Stopping here is what keeps a blow-up cheap: the alternative is
            # grinding to ITER= 5000 or wedging on an aborted MPI job.
            return True
        return allow_early_stop and monitor.should_stop(parser)

    outcome: RunOutcome = runner.run(
        config_path=config_path,
        working_directory=working_directory,
        ranks=solver.mpi_ranks,
        on_line=handle_line,
        should_stop=should_stop,
        stall_timeout_s=solver.stall_timeout_s,
        wall_time_limit_s=solver.wall_time_limit_s,
    )

    # A divergence message can arrive after the last line the predicate saw,
    # so the parser gets the final word.
    if not state["diverged"] and monitor.diverged(parser):
        state["diverged"] = True
        state["reason"] = monitor.reason

    return _StageOutcome(
        parser=parser,
        monitor=monitor,
        outcome=outcome,
        diverged=bool(state["diverged"]),
        divergence_reason=str(state["reason"]),
    )


def _rescue(
    request: AeroRunRequest,
    first_attempt: _StageOutcome,
    working_directory: Path,
    run_stage: callable,
    on_line: callable | None,
) -> tuple[_StageOutcome, list[str]]:
    """Retry a diverged solve as first order, then restart at second order.

    A cold supersonic start is the fragile part; once a first-order solution
    exists there is a shock in roughly the right place and the second-order
    scheme has something sane to reconstruct from.
    """
    solver = request.solver
    notes = [
        f"The first attempt diverged ({first_attempt.divergence_reason}). "
        f"It was retried automatically as a first-order stage at CFL "
        f"{solver.rescue_cfl:g} for {solver.rescue_iterations} iterations, "
        "followed by a second-order restart from that solution."
    ]
    _announce(on_line, "rescue-a", notes[0])

    stage_a = run_stage(
        replace(
            solver,
            muscl=False,
            cfl_number=solver.rescue_cfl,
            max_iterations=solver.rescue_iterations,
            restart=False,
        ),
        RESCUE_A_CONFIG_FILENAME,
        # Divergence detection only. The force-steadiness predicate trips
        # easily on a first-order stage, where drag plateaus quickly and
        # falsely -- and a stage killed mid-run never writes the final restart
        # that stage B needs.
        allow_early_stop=False,
        history_filename="history_firstorder",
        write_frequency=100,
    )
    if stage_a.diverged:
        raise AeroSolverError(
            "the solve diverged twice: at second order "
            f"({first_attempt.divergence_reason}), and again at first order, "
            f"CFL {solver.rescue_cfl:g} ({stage_a.divergence_reason}). This is "
            "a mesh or boundary-condition problem rather than a numerics one "
            "-- check the mesh quality report and the freestream conditions."
        )

    restart = working_directory / RESTART_FILENAME
    if not restart.is_file():
        raise AeroSolverError(
            "the first-order rescue stage finished but wrote no restart file "
            f"({RESTART_FILENAME}), so the second-order stage has nothing to "
            "continue from."
        )
    solution = working_directory / SOLUTION_FILENAME
    solution.write_bytes(restart.read_bytes())

    # A stale breakdown from stage A would otherwise be picked up silently if
    # stage B failed before writing its own, reporting a first-order number as
    # if it were the answer.
    breakdown = working_directory / FORCES_BREAKDOWN_FILENAME
    if breakdown.is_file():
        breakdown.unlink()

    _announce(
        on_line,
        "rescue-b",
        "First-order stage converged; restarting at second order.",
    )
    stage_b = run_stage(
        replace(solver, restart=True),
        RESCUE_B_CONFIG_FILENAME,
        allow_early_stop=True,
        restart_from=SOLUTION_FILENAME,
    )
    if stage_b.diverged:
        raise AeroSolverError(
            "the second-order stage diverged even when restarted from a "
            f"converged first-order solution ({stage_b.divergence_reason}). "
            "Lower the CFL number or turn off MUSCL reconstruction for this "
            "case."
        )
    stage_b.rescued = True
    return stage_b, notes


def replace(solver: SolverParams, **changes) -> SolverParams:
    """A ``dataclasses.replace`` for the Pydantic solver settings.

    Reconstructing rather than copying keeps the field validators in play, so
    a rescue cannot quietly produce settings the operator could not have typed
    themselves.
    """
    return SolverParams(**{**solver.model_dump(), **changes})


def _announce(on_line: callable | None, stage: str, message: str) -> None:
    """Put a stage boundary on the line stream, for the log and the plot."""
    if on_line is not None:
        on_line(f"{STAGE_MARKER_PREFIX} {stage}")
        on_line(message)


def _warning_notes(parser: SU2OutputParser) -> list[str]:
    """Solver warnings worth carrying into the result.

    SU2 telling the operator something about their mesh is not ours to
    swallow, which is what used to happen to every one of these.
    """
    return [f"Solver warning: {text}" for text in parser.warnings[:5]]


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
    """Rotate wind-axis forces into the mesh frame.

    SU2 defines drag along the freestream direction, which is inclined from
    the body axis by the angle of attack and sideslip.

    The frame here is the mesh's, not the textbook body frame, and the two
    are not the same: meshing places the nose at minimum X with the flow
    running in +X, so drag pushes the body towards +X. The classical
    wind-to-body matrix assumes a body axis pointing forward out of the
    nose and returns the negatives of these components -- which is fine
    until the result is combined with SU2's moments, which arrive in the
    mesh frame, and the centre of pressure lands on the wrong side of the
    rocket.

    At zero incidence this reduces to a pure drag force along +X and
    nothing else, which is the check worth remembering.
    """
    alpha = math.radians(flow.aoa_deg)
    beta = math.radians(flow.sideslip_deg)

    cos_a, sin_a = math.cos(alpha), math.sin(alpha)
    cos_b, sin_b = math.cos(beta), math.sin(beta)

    # Freestream direction, and the lift and side directions perpendicular
    # to it, all expressed in the mesh frame.
    x = drag_n * cos_a * cos_b - lift_n * sin_a - sideforce_n * cos_a * sin_b
    y = drag_n * sin_b + sideforce_n * cos_b
    z = drag_n * sin_a * cos_b + lift_n * cos_a - sideforce_n * sin_a * sin_b
    return np.array([x, y, z])
