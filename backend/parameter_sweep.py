"""One-variable sweeps of the sensor-study models at fixed steps.

A design study answers "how reliable is it over the whole uncertain range";
a sweep answers the simpler question "what happens as I turn this one knob"
-- wind 0.2 to 5 m/s every 0.2 m/s, say. Every point of a sweep is solved
directly with the analytical model: nothing is read off a response surface,
so there is no fitting error and nothing is extrapolated beyond the range a
surface was fitted on (a quadratic surface fitted on 0.5-5 m/s and read at
0.2 m/s can be off by half its value, and it invents minima a smooth
physical curve does not have).

The other inputs stay at the setup's baseline values (``wind_speed_ms``,
``solar_flux_w_m2``, ``bottom_flux_w_m2`` of a shield setup; ``speed_ms``,
``yaw_deg``, ``droplet_um`` of an SPS30 setup).
"""

from __future__ import annotations

import csv
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_SWEEP_POINTS = 2001

SHIELD_INPUTS = ("wind_speed_ms", "solar_flux_w_m2", "bottom_flux_w_m2")
SHIELD_OUTPUTS = {
    "delta_t_k": "T_monitor - T_inlet [K]",
    "shield_temp_k": "Shield temperature [K]",
    "monitor_temp_k": "Monitor (thermometer) air temperature [K]",
}
SPS30_INPUTS = ("speed_ms", "yaw_deg", "droplet_um")
SPS30_OUTPUTS = {
    "face_velocity_ms": "Max velocity at sensor face [m/s]",
    "penetration": "Droplet penetration to sensor face [-]",
    "exchange_flow_lpm": "Chamber exchange flow [L/min]",
    "port_flow_lpm": "Flow through the side slits [L/min]",
}
INPUT_LABELS = {
    "wind_speed_ms": "Wind speed [m/s]",
    "solar_flux_w_m2": "Solar flux [W/m2]",
    "bottom_flux_w_m2": "Bottom flux [W/m2]",
    "speed_ms": "Platform speed [m/s]",
    "yaw_deg": "Yaw [deg]",
    "droplet_um": "Droplet diameter [um]",
}


class SweepError(ValueError):
    """The sweep was asked for with impossible settings."""


@dataclass
class SweepResult:
    """The solved sweep: one row per value, inputs and outputs together."""

    kind: str
    variable: str
    fixed: dict[str, float]
    rows: list[dict[str, float]]
    outputs: dict[str, str]
    notes: list[str] = field(default_factory=list)
    csv_path: str | None = None
    image_path: str | None = None

    def column(self, name: str) -> list[float]:
        return [row[name] for row in self.rows]


def sweep_values(start: float, stop: float, step: float) -> list[float]:
    """start, start+step, ... up to and including stop (within rounding)."""
    if not all(math.isfinite(v) for v in (start, stop, step)):
        raise SweepError("start, stop and step must be numbers")
    if step <= 0.0:
        raise SweepError("step must be positive")
    if stop < start:
        raise SweepError("stop must not be below start")
    count = int(math.floor((stop - start) / step + 1e-9)) + 1
    if count > MAX_SWEEP_POINTS:
        raise SweepError(
            f"{count} points is too many; use a larger step (at most {MAX_SWEEP_POINTS})"
        )
    decimals = max(0, -int(math.floor(math.log10(step))) + 3)
    values = [round(start + i * step, decimals) for i in range(count)]
    if stop - values[-1] > 1e-9 * max(1.0, abs(stop)):
        values.append(round(stop, decimals))
    return values


def shield_sweep(setup, variable: str, start: float, stop: float, step: float) -> SweepResult:
    """Solve the shield's analytical model at every value of one input."""
    from backend.shield_study import ShieldAnalyticModel

    if variable not in SHIELD_INPUTS:
        raise SweepError(f"unknown variable '{variable}'; use {', '.join(SHIELD_INPUTS)}")
    values = sweep_values(start, stop, step)
    if variable == "wind_speed_ms" and values[0] <= 0.0:
        raise SweepError("wind speed must be above 0 m/s")
    if variable != "wind_speed_ms" and values[0] < 0.0:
        raise SweepError("radiation fluxes cannot be negative")
    model = ShieldAnalyticModel(setup)
    fixed = {name: float(getattr(setup, name)) for name in SHIELD_INPUTS if name != variable}
    rows = []
    for value in values:
        inputs = {variable: value, **fixed}
        balance = model.solve(
            inputs["wind_speed_ms"], inputs["solar_flux_w_m2"], inputs["bottom_flux_w_m2"]
        )
        rows.append(
            {
                **{name: inputs[name] for name in SHIELD_INPUTS},
                "delta_t_k": balance.delta_t_k,
                "shield_temp_k": balance.shield_temp_k,
                "monitor_temp_k": balance.monitor_temp_k,
            }
        )
    notes = [
        "Every point solved directly with the analytical (lumped) model -- a "
        "screening estimate, not CFD; confirm the interesting points with "
        "prepare_cfd/solve_cfd or Fluent."
    ]
    if variable == "wind_speed_ms" and values[0] < 0.5:
        notes.append(
            "Below about 0.5 m/s natural convection takes over and the lumped "
            "model is least certain; treat these points with most caution."
        )
    return SweepResult("shield", variable, fixed, rows, dict(SHIELD_OUTPUTS), notes)


def sps30_sweep(setup, variable: str, start: float, stop: float, step: float) -> SweepResult:
    """Solve the SPS30 housing's analytical model at every value of one input."""
    from backend.sps30_study import Sps30AnalyticModel

    if variable not in SPS30_INPUTS:
        raise SweepError(f"unknown variable '{variable}'; use {', '.join(SPS30_INPUTS)}")
    values = sweep_values(start, stop, step)
    if variable in ("speed_ms", "droplet_um") and values[0] <= 0.0:
        raise SweepError(f"{variable} must be above 0")
    model = Sps30AnalyticModel(setup)
    fixed = {name: float(getattr(setup, name)) for name in SPS30_INPUTS if name != variable}
    rows = []
    for value in values:
        inputs = {variable: value, **fixed}
        solved = model.solve(inputs["speed_ms"], inputs["yaw_deg"], inputs["droplet_um"])
        rows.append(
            {
                **{name: inputs[name] for name in SPS30_INPUTS},
                **{name: float(solved[name]) for name in SPS30_OUTPUTS},
            }
        )
    notes = [
        "Every point solved directly with the analytical (lumped) model -- a "
        "screening estimate, not CFD; confirm the interesting points with "
        "prepare_cfd/solve_cfd or Fluent.",
        f"Goals: face velocity below {setup.max_face_velocity_ms:g} m/s and "
        f"penetration at most {setup.max_penetration:g}.",
    ]
    return SweepResult("sps30", variable, fixed, rows, dict(SPS30_OUTPUTS), notes)


def save_sweep(result: SweepResult, folder: Path | str, plot: bool = True) -> SweepResult:
    """Write the sweep as CSV (and a PNG chart) into a new folder under ``folder``."""
    folder = Path(folder) / f"{result.kind}-{result.variable}-{time.strftime('%Y%m%d-%H%M%S')}"
    stem, counter = folder, 2
    while folder.exists():
        folder = Path(f"{stem}-{counter}")
        counter += 1
    folder.mkdir(parents=True)
    columns = list(result.rows[0].keys())
    path = folder / "sweep.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(result.rows)
    result.csv_path = str(path)
    if plot:
        result.image_path = str(plot_sweep(result, folder / "sweep.png"))
    return result


def plot_sweep(result: SweepResult, output_path: Path | str) -> Path:
    """One small chart per output against the swept input."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names = [n for n in result.outputs if n != "shield_temp_k" and n != "monitor_temp_k"]
    figure, axes = plt.subplots(1, len(names), figsize=(4.4 * len(names), 3.4), squeeze=False)
    x = result.column(result.variable)
    for axis, name in zip(axes[0], names):
        axis.plot(x, result.column(name), "o-", markersize=3, color="#1f77b4")
        axis.set_xlabel(INPUT_LABELS[result.variable])
        axis.set_ylabel(result.outputs[name])
        axis.grid(True, alpha=0.3)
        if name == "delta_t_k":
            axis.axhline(0.0, color="grey", linewidth=0.8)
    fixed = ", ".join(f"{INPUT_LABELS[k].split(' [')[0].lower()} {v:g}" for k, v in result.fixed.items())
    figure.suptitle(f"{'Radiation shield' if result.kind == 'shield' else 'SPS30 housing'} "
                    f"sweep, analytical model\nheld at: {fixed}", fontsize=9)
    figure.tight_layout()
    output_path = Path(output_path)
    figure.savefig(output_path, dpi=110)
    plt.close(figure)
    return output_path


def as_dict(result: SweepResult, max_rows: int = 400) -> dict[str, Any]:
    """The sweep for a tool reply: rows rounded to readable precision."""
    rows = [
        {k: (round(v, 6) if isinstance(v, float) else v) for k, v in row.items()}
        for row in result.rows[:max_rows]
    ]
    return {
        "kind": result.kind,
        "variable": result.variable,
        "points": len(result.rows),
        "held_fixed": result.fixed,
        "outputs": result.outputs,
        "rows": rows,
        "rows_truncated": len(result.rows) > max_rows,
        "csv_path": result.csv_path,
        "image_path": result.image_path,
        "notes": result.notes,
    }
