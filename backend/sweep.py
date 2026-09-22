"""Parametric sweeps over flow conditions.

Batch runs are the reason the platform exists: a drag polar or a
hinge-torque-versus-Mach curve needs a dozen solves, not one. Cases run
sequentially -- 16 GB will not hold two 750k-cell RANS solutions at once --
and the mesh is reused across points whenever the geometry has not changed,
which is what makes a sweep cost one meshing pass plus N solves rather than N
of each.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from backend.aero_solver import run_aero_case
from backend.runner import SolverRunner
from core.models import AeroResult, AeroRunRequest, FlowParams

# Parameters a sweep may vary, mapped onto the FlowParams field they set.
SWEEPABLE_PARAMETERS: dict[str, str] = {
    "mach": "velocity_value",
    "velocity": "velocity_value",
    "velocity_value": "velocity_value",
    "aoa": "aoa_deg",
    "aoa_deg": "aoa_deg",
    "alpha": "aoa_deg",
    "sideslip": "sideslip_deg",
    "sideslip_deg": "sideslip_deg",
    "beta": "sideslip_deg",
    "altitude": "altitude_m",
    "altitude_m": "altitude_m",
}


class SweepError(RuntimeError):
    """Raised when a sweep cannot be set up or completed."""


@dataclass
class SweepPoint:
    """One completed or failed point of a sweep."""

    value: float
    result: AeroResult | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """True when this point produced a result."""
        return self.result is not None

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable summary of this point."""
        if self.result is None:
            return {"value": self.value, "succeeded": False, "error": self.error}
        return {
            "value": self.value,
            "succeeded": True,
            "cd": self.result.cd,
            "cl": self.result.cl,
            "cs": self.result.cs,
            "cm_pitch": self.result.cm_pitch,
            "drag_n": self.result.drag_n,
            "lift_n": self.result.lift_n,
            "mach": self.result.mach,
            "aoa_deg": self.result.aoa_deg,
            "center_of_pressure": self.result.center_of_pressure,
            "force_rocket_n": self.result.force_rocket_n,
            "center_of_pressure_rocket": self.result.center_of_pressure_rocket,
            "hinge_torques": {
                torque.name: torque.torque_nm
                for torque in self.result.hinge_torques
            },
            "sim_id": self.result.sim_id,
            "converged": self.result.converged,
            "wall_time_s": self.result.wall_time_s,
        }


@dataclass
class SweepResult:
    """Aggregate output of a parametric sweep."""

    parameter: str
    values: list[float]
    points: list[SweepPoint] = field(default_factory=list)
    wall_time_s: float = 0.0

    @property
    def succeeded_points(self) -> list[SweepPoint]:
        """Points that produced a result."""
        return [point for point in self.points if point.succeeded]

    @property
    def failed_points(self) -> list[SweepPoint]:
        """Points that failed, with their error messages."""
        return [point for point in self.points if not point.succeeded]

    def curve(self, quantity: str) -> tuple[list[float], list[float]]:
        """Extract an (x, y) curve for a scalar quantity.

        Parameters
        ----------
        quantity:
            Result attribute, e.g. ``cd``, ``cl``, or a hinge name prefixed
            with ``torque:``.

        Returns
        -------
        tuple
            Swept values and the corresponding quantity, failed points
            omitted.
        """
        xs: list[float] = []
        ys: list[float] = []
        for point in self.points:
            if point.result is None:
                continue
            value = _extract_quantity(point.result, quantity)
            if value is None or not math.isfinite(value):
                continue
            xs.append(point.value)
            ys.append(value)
        return xs, ys

    def summary_curves(self) -> dict[str, list[float]]:
        """Every standard curve in one dictionary, for the MCP response."""
        curves: dict[str, list[float]] = {"values": []}
        names = ("cd", "cl", "cs", "cm_pitch", "drag_n", "lift_n")
        for name in names:
            curves[name] = []

        hinge_names: list[str] = []
        for point in self.succeeded_points:
            assert point.result is not None
            for torque in point.result.hinge_torques:
                if torque.name not in hinge_names:
                    hinge_names.append(torque.name)
        for name in hinge_names:
            curves[f"torque_{name}_nm"] = []

        for point in self.points:
            if point.result is None:
                continue
            curves["values"].append(point.value)
            for name in names:
                curves[name].append(float(getattr(point.result, name)))
            torques = {
                torque.name: torque.torque_nm
                for torque in point.result.hinge_torques
            }
            for name in hinge_names:
                curves[f"torque_{name}_nm"].append(
                    float(torques.get(name, math.nan))
                )
        return curves

    def extremes(self) -> dict[str, Any]:
        """Peak values a designer sizes hardware from.

        The maximum hinge torque across a sweep is the number a servo must be
        chosen for, so it is surfaced explicitly rather than left for the
        caller to find.
        """
        results = [point.result for point in self.succeeded_points if point.result]
        if not results:
            return {}

        max_drag = max(results, key=lambda r: r.drag_n)
        max_lift = max(results, key=lambda r: abs(r.lift_n))
        summary: dict[str, Any] = {
            "max_drag_n": max_drag.drag_n,
            "max_drag_at": max_drag.mach,
            "max_abs_lift_n": abs(max_lift.lift_n),
            "max_cd": max(r.cd for r in results),
            "min_cd": min(r.cd for r in results),
        }

        torque_peaks: dict[str, float] = {}
        for result in results:
            for torque in result.hinge_torques:
                magnitude = abs(torque.torque_nm)
                if magnitude > torque_peaks.get(torque.name, -math.inf):
                    torque_peaks[torque.name] = magnitude
        if torque_peaks:
            summary["max_abs_hinge_torque_nm"] = torque_peaks
            summary["servo_sizing_torque_nm"] = max(torque_peaks.values())
        return summary

    def as_dict(self) -> dict[str, Any]:
        """Full JSON-serialisable sweep report."""
        return {
            "parameter": self.parameter,
            "values": self.values,
            "points": [point.as_dict() for point in self.points],
            "curves": self.summary_curves(),
            "extremes": self.extremes(),
            "succeeded": len(self.succeeded_points),
            "failed": len(self.failed_points),
            "wall_time_s": self.wall_time_s,
        }


def _extract_quantity(result: AeroResult, quantity: str) -> float | None:
    """Pull a named scalar out of a result, including hinge torques."""
    if quantity.startswith("torque:"):
        name = quantity.split(":", 1)[1]
        for torque in result.hinge_torques:
            if torque.name == name:
                return torque.torque_nm
        return None
    value = getattr(result, quantity, None)
    return float(value) if isinstance(value, (int, float)) else None


def resolve_parameter(name: str) -> str:
    """Map a user-facing parameter name onto a FlowParams field.

    Raises
    ------
    SweepError
        If the name is not sweepable, listing the ones that are.
    """
    key = name.strip().lower()
    if key not in SWEEPABLE_PARAMETERS:
        raise SweepError(
            f"'{name}' is not a sweepable parameter. Choose one of: "
            f"{', '.join(sorted(set(SWEEPABLE_PARAMETERS)))}"
        )
    return SWEEPABLE_PARAMETERS[key]


def run_parametric_sweep(
    base_request: AeroRunRequest,
    parameter: str,
    values: Sequence[float],
    mesh_path: Path | str,
    output_root: Path | str,
    runner: SolverRunner,
    reference_area_m2: float,
    reference_length_m: float,
    on_point: Callable[[SweepPoint], None] | None = None,
    stop_on_error: bool = False,
) -> SweepResult:
    """Run one simulation per value of a swept parameter.

    Points run sequentially and share one mesh. A point that fails is
    recorded and the sweep continues, so a single non-converging condition
    does not discard the rest of an expensive batch.

    Parameters
    ----------
    base_request:
        Template request; the swept field is overridden per point.
    parameter:
        Name of the parameter to vary, e.g. ``mach`` or ``aoa``.
    values:
        Values to run.
    mesh_path:
        Shared mesh, reused for every point.
    output_root:
        Directory under which each point gets its own run directory.
    runner:
        Solver backend.
    reference_area_m2, reference_length_m:
        Reference quantities for coefficient normalisation.
    on_point:
        Called after each point, for progress reporting.
    stop_on_error:
        Abort the sweep at the first failure instead of continuing.

    Returns
    -------
    SweepResult
        Every point, the summary curves and the peak values.
    """
    field_name = resolve_parameter(parameter)
    values = [float(value) for value in values]
    if not values:
        raise SweepError("a sweep needs at least one value")

    mesh_path = Path(mesh_path)
    if not mesh_path.is_file():
        raise SweepError(f"mesh file not found: {mesh_path}")

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    sweep = SweepResult(parameter=parameter, values=values)

    for index, value in enumerate(values):
        point = SweepPoint(value=value)
        try:
            request = _request_for_value(base_request, field_name, value)
            work = output_root / f"point_{index:03d}"
            point.result = run_aero_case(
                request,
                mesh_path=mesh_path,
                working_directory=work,
                runner=runner,
                reference_area_m2=reference_area_m2,
                reference_length_m=reference_length_m,
                sim_id=f"{output_root.name}-{index:03d}",
            )
        except Exception as error:  # noqa: BLE001 - recorded, not swallowed
            point.error = f"{type(error).__name__}: {error}"

        sweep.points.append(point)
        if on_point is not None:
            on_point(point)
        if stop_on_error and not point.succeeded:
            break

    sweep.wall_time_s = time.perf_counter() - started
    return sweep


def _request_for_value(
    base: AeroRunRequest, field_name: str, value: float
) -> AeroRunRequest:
    """Copy a request with one flow field replaced."""
    flow_data = base.flow.model_dump()
    flow_data[field_name] = value
    try:
        flow = FlowParams(**flow_data)
    except Exception as error:
        raise SweepError(
            f"value {value} is not valid for '{field_name}': {error}"
        ) from error

    data = base.model_dump()
    data["flow"] = flow.model_dump()
    return AeroRunRequest(**data)


def linear_values(start: float, stop: float, count: int) -> list[float]:
    """Evenly spaced sweep values, inclusive of both endpoints."""
    if count < 1:
        raise SweepError(f"count must be at least 1, got {count}")
    if count == 1:
        return [float(start)]
    return [float(v) for v in np.linspace(start, stop, count)]
