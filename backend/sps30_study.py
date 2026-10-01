"""SPS30 housing study: lumped model, DoE, response surfaces, Monte Carlo.

The lumped model is a screening tool built from textbook relations:

* **Cross-flow through the static ports.** At zero yaw both side slits sit
  at the same static pressure and drive almost nothing; with yaw the
  windward side's pressure coefficient rises, the leeward one falls, and
  the difference pushes air through two orifices in series:
  ``Q = Cd A sqrt(dp / rho)``, ``dp = 1/2 rho V^2 * 2 |sin(yaw)|``.
* **Face velocity.** The SPS30 fan's own intake velocity plus the share of
  the cross-flow that detours through the sensor chamber, with a peak
  factor of two.
* **Droplets.** A droplet reaches the sensor only if it (1) turns 90
  degrees into a flush slit (aspiration efficiency falls with the Stokes
  number on the slit width, except the ballistic share a windward slit
  collects at yaw, which crosses the plenum into a wall), (2) is not
  settled out by gravity in the plenum,
  (3) makes the turn round the baffle (impactor efficiency with a
  50 % cut at Stk = 0.59, rectangular jet), and (4) is drawn into the face
  by the fan. Penetration is the share of the droplets *that got inside
  the housing* which reach the face. Fine mist at low speed passes all four -- physics, not a
  model artefact: inertial impaction cannot stop droplets whose Stokes
  number is far below the cut.
"""

from __future__ import annotations

import csv
import math
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from backend.design_exploration import (
    Axis,
    ExplorationError,
    Surface,
    central_composite,
    decode,
    encode,
    latin_hypercube,
)
from core.sps30_models import (
    SPS30_OUTPUTS,
    SPS30_VARIABLES,
    OutputSummary,
    Sps30Evaluator,
    Sps30Point,
    Sps30Result,
    Sps30Setup,
    Sps30StudyParams,
)

GRAVITY = 9.81
PORT_DISCHARGE = 0.62
# Share of the free-stream dynamic pressure that turbulence and the
# housing's wake pump through the ports even at zero yaw.
TURBULENT_PUMPING = 0.02
IMPACTOR_STK50 = 0.59


class Sps30StudyError(RuntimeError):
    """A study could not be set up or completed."""


def air_properties(temp_c: float) -> tuple[float, float]:
    """Density and dynamic viscosity of air at sea level."""
    temp = temp_c + 273.15
    density = 101325.0 / (287.05 * temp)
    viscosity = 1.716e-5 * (temp / 273.15) ** 1.5 * (273.15 + 110.4) / (temp + 110.4)
    return density, viscosity


def relaxation_time(diameter_m: float, water: float, viscosity: float) -> float:
    """Droplet response time (Stokes), s."""
    return water * diameter_m**2 / (18.0 * viscosity)


def terminal_velocity(diameter_m: float, water: float, air: float, viscosity: float) -> float:
    """Settling speed with the Schiller-Naumann drag correction, m/s."""
    velocity = relaxation_time(diameter_m, water, viscosity) * GRAVITY
    for _ in range(50):
        reynolds = air * velocity * diameter_m / viscosity
        correction = 1.0 + 0.15 * reynolds**0.687
        velocity = relaxation_time(diameter_m, water, viscosity) * GRAVITY / correction
    return min(velocity, 9.0)


class Sps30AnalyticModel:
    """Lumped model of the housing; see the module docstring."""

    def __init__(self, setup: Sps30Setup) -> None:
        self.setup = setup
        self.air, self.viscosity = air_properties(setup.ambient_temp_c)

    def solve(self, speed: float, yaw_deg: float, droplet_um: float) -> dict[str, float]:
        s = self.setup
        rho = self.air
        yaw = math.radians(yaw_deg)
        dp = 0.5 * rho * speed**2 * 2.0 * abs(math.sin(yaw))
        cross = PORT_DISCHARGE * s.port_area_m2 * math.sqrt(dp / rho)
        pumping = TURBULENT_PUMPING * s.port_area_m2 * speed
        ports = cross + pumping
        fan = s.fan_flow_m3s()
        chamber = s.chamber_fraction * ports
        exchange = chamber + fan
        face = fan / s.face_area_m2() + 2.0 * chamber / s.chamber_area_m2
        penetration = self._penetration(speed, yaw, droplet_um * 1e-6, ports, chamber, exchange)
        return {
            "face_velocity_ms": face,
            "penetration": penetration,
            "exchange_flow_lpm": exchange * 60000.0,
            "port_flow_lpm": ports * 60000.0,
        }

    def _penetration(
        self, speed, yaw, diameter, ports, chamber, exchange
    ) -> float:
        s = self.setup
        tau = relaxation_time(diameter, s.water_density_kg_m3, self.viscosity)
        # (1) into a flush slit: a 90-degree turn, plus the ballistic share
        # a windward slit collects when the wind is yawed.
        stk_port = tau * speed / s.port_width_m
        turning = 1.0 / (1.0 + 4.0 * stk_port)
        ballistic = abs(math.sin(yaw)) * (1.0 - turning)
        # Of the droplets that get inside, the share that came in with the
        # air (the rest flew in ballistically and cross the plenum into a
        # wall rather than following the air round the baffle).
        frontal = self._frontal_area()
        feed = min(1.0, ports / max(speed * frontal, 1e-12)) * turning
        stray = ballistic * s.port_area_m2 / frontal
        following = feed / max(feed + stray, 1e-300)
        # (2) settling in the plenum, treated as well mixed.
        settle_speed = terminal_velocity(diameter, s.water_density_kg_m3, self.air, self.viscosity)
        plenum_height = math.sqrt(s.plenum_area_m2)
        floor = s.plenum_area_m2 * 0.7
        settled = 1.0 - math.exp(-settle_speed * floor / max(ports, 1e-12))
        # (3) the turn round the baffle: an impactor.
        width = s.plenum_area_m2 / plenum_height
        gap_speed = chamber / max(s.baffle_gap_m * width, 1e-12)
        stk = 2.0 * tau * gap_speed / s.baffle_gap_m
        impacted = stk**3 / (stk**3 + IMPACTOR_STK50**3)
        route = s.chamber_fraction * following
        # (4) drawn into the face by the fan rather than carried on through.
        drawn = s.fan_flow_m3s() / max(exchange, 1e-12)
        return float(min(1.0, route * (1.0 - settled) * (1.0 - impacted) * drawn))

    def _frontal_area(self) -> float:
        s = self.setup
        return max(s.chamber_area_m2, 1e-6)


# ---------------------------------------------------------------------------
# Design points, surfaces, Monte Carlo
# ---------------------------------------------------------------------------


def axes(params: Sps30StudyParams) -> list[Axis]:
    return [
        Axis(
            name=name, minimum=v.minimum, maximum=v.maximum, log=v.log,
            distribution=v.distribution, mean=v.mean, std=v.std, mode=v.mode,
        )
        for name, v in params.variables().items()
    ]


def design_points(params: Sps30StudyParams) -> list[Sps30Point]:
    coded = central_composite(3) if params.doe == "ccd" else latin_hypercube(3, params.doe_points, params.seed)
    physical = decode(axes(params), coded)
    return [
        Sps30Point(
            name=f"DP{i}",
            speed_ms=round(float(r[0]), 6),
            yaw_deg=round(float(r[1]), 6),
            droplet_um=round(float(r[2]), 6),
        )
        for i, r in enumerate(physical)
    ]


PointSolver = Callable[[Sps30Point], dict[str, float]]


def analytic_solver(setup: Sps30Setup) -> PointSolver:
    model = Sps30AnalyticModel(setup)
    return lambda p: model.solve(*p.inputs())


def solve_points(points: Sequence[Sps30Point], solver: PointSolver, source: str) -> list[Sps30Point]:
    out = []
    for point in points:
        if not point.solved():
            values = solver(point)
            point = point.model_copy(update={k: float(values[k]) for k in SPS30_OUTPUTS} | {"source": source})
        out.append(point)
    return out


def _goal_flags(setup: Sps30Setup, values: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    flags = {
        "face_velocity": values["face_velocity_ms"] <= setup.max_face_velocity_ms,
        # Surfaces through zeros are not exactly zero between them.
        "penetration": values["penetration"] <= setup.max_penetration + 1e-4,
    }
    if setup.min_exchange_flow_lpm > 0.0:
        flags["exchange_flow"] = values["exchange_flow_lpm"] >= setup.min_exchange_flow_lpm
    return flags


def analyse(
    params: Sps30StudyParams, points: Sequence[Sps30Point], evaluator: Sps30Evaluator
) -> Sps30Result:
    from scipy.stats import spearmanr

    solved = [p for p in points if p.solved()]
    ax = axes(params)
    coded = encode(ax, np.array([p.inputs() for p in solved])) if solved else np.zeros((0, 3))
    rng = np.random.default_rng(params.seed)
    inputs = np.column_stack([a.sample(params.monte_carlo_samples, rng) for a in ax])
    sample_coded = encode(ax, inputs)

    predictions: dict[str, np.ndarray] = {}
    outputs: dict[str, OutputSummary] = {}
    for name in SPS30_OUTPUTS:
        values = np.array([getattr(p, name) for p in solved], dtype=float)
        try:
            surface = Surface(coded, values, params.surrogate)
        except ExplorationError as error:
            raise Sps30StudyError(str(error)) from error
        predicted = surface.predict(sample_coded)
        predicted = np.clip(predicted, 0.0, 1.0) if name == "penetration" else np.maximum(predicted, 0.0)
        predictions[name] = predicted
        worst = int(np.argmin(predicted)) if name == "exchange_flow_lpm" else int(np.argmax(predicted))
        sens = {}
        for i, var in enumerate(SPS30_VARIABLES):
            rho = spearmanr(inputs[:, i], predicted).statistic
            sens[var] = float(0.0 if np.isnan(rho) else rho)
        counts, edges = np.histogram(predicted, bins=40)
        outputs[name] = OutputSummary(
            name=name,
            r_squared=surface.r_squared(),
            loo_rmse=surface.leave_one_out(),
            mean=float(predicted.mean()),
            p05=float(np.percentile(predicted, 5)),
            p95=float(np.percentile(predicted, 95)),
            minimum=float(predicted.min()),
            maximum=float(predicted.max()),
            worst_case={**{v: float(inputs[worst, i]) for i, v in enumerate(SPS30_VARIABLES)},
                        name: float(predicted[worst])},
            sensitivities=sens,
            histogram_counts=[int(c) for c in counts],
            histogram_edges=[float(e) for e in edges],
        )

    setup = params.setup
    flags = _goal_flags(setup, predictions)
    passing = np.logical_and.reduce(list(flags.values()))
    first_failure = None
    if not passing.all():
        violation = np.maximum(predictions["face_velocity_ms"] / setup.max_face_velocity_ms - 1.0, 0.0)
        violation = violation + np.maximum(predictions["penetration"] - setup.max_penetration, 0.0) * 100.0
        index = int(np.argmax(np.where(passing, -1.0, violation)))
        first_failure = {
            **{v: float(inputs[index, i]) for i, v in enumerate(SPS30_VARIABLES)},
            **{o: float(predictions[o][index]) for o in SPS30_OUTPUTS},
        }

    notes = []
    fine = [p for p in solved if p.penetration and p.penetration > setup.max_penetration]
    if fine:
        smallest = min(p.droplet_um for p in fine)
        notes.append(
            f"Droplets reached the sensor face at {len(fine)} design point(s), down to "
            f"{smallest:.0f} um. Inertial separation cannot stop droplets whose Stokes "
            "number at the baffle is far below ~0.6; fine mist needs a filter, a longer "
            "labyrinth or a lower sampling velocity, not a sharper turn alone."
        )
    for name, summary in outputs.items():
        values = np.array([getattr(p, name) for p in solved], dtype=float)
        spread = float(values.max() - values.min()) if len(values) else 0.0
        if spread > 0 and not math.isnan(summary.loo_rmse) and summary.loo_rmse > 0.25 * spread:
            notes.append(
                f"The {name.replace('_', ' ')} surface predicts left-out points only to within "
                f"{summary.loo_rmse:.3g} (range {spread:.3g}); its extremes are unreliable. "
                "Add design points (Latin hypercube, 30+) or switch the surface type."
            )
    if evaluator is Sps30Evaluator.ANALYTIC:
        notes.append(
            "Solved with the lumped screening model; run the design points with SU2 "
            "or Fluent for design-grade numbers."
        )
    return Sps30Result(
        params=params,
        evaluator=evaluator,
        points=list(points),
        outputs=outputs,
        samples=len(inputs),
        reliability=float(passing.mean()),
        reliability_by_goal={k: float(v.mean()) for k, v in flags.items()},
        first_failure=first_failure,
        notes=notes,
    )


def run_analytic_study(params: Sps30StudyParams) -> Sps30Result:
    points = solve_points(design_points(params), analytic_solver(params.setup), "analytic")
    return analyse(params, points, Sps30Evaluator.ANALYTIC)


# ---------------------------------------------------------------------------
# Design-point tables
# ---------------------------------------------------------------------------

CSV_COLUMNS = (
    "name", "speed_ms", "yaw_deg", "droplet_um",
    "face_velocity_ms", "penetration", "exchange_flow_lpm", "sensor_hits", "droplets",
)

_ALIASES = {
    "name": ("name", "design point", "dp"),
    "speed_ms": ("speed_ms", "platform_speed", "inlet_velocity", "speed", "velocity"),
    "yaw_deg": ("yaw_deg", "yaw", "aoa", "angle"),
    "droplet_um": ("droplet_um", "droplet_diameter", "diameter", "droplet"),
    "face_velocity_ms": ("face_velocity_ms", "max_face_velocity", "face_velocity"),
    "penetration": ("penetration",),
    "sensor_hits": ("sensor_hits", "trap_count", "dpm_trap_count", "sensor_trap_count"),
    "droplets": ("droplets", "injected", "injected_droplets"),
    "exchange_flow_lpm": ("exchange_flow_lpm", "exchange_flow"),
    "mass_flow": ("mass_flow", "chamber_mass_flow", "mass_flow_rate"),
    "ux_integral": ("ux_integral",),
}


def write_points_csv(path: Path | str, points: Sequence[Sps30Point]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for p in points:
            row = [getattr(p, c) for c in CSV_COLUMNS]
            writer.writerow(["" if v is None else (f"{v:.6g}" if isinstance(v, float) else v) for v in row])
    return path


def _find(header: list[str], field: str) -> tuple[int | None, str]:
    for alias in _ALIASES[field]:
        for index, cell in enumerate(header):
            text = cell.strip().lower()
            name = text.split(" - ", 1)[-1].split("[", 1)[0].strip()
            if name == alias:
                unit = text.split("[", 1)[1].rstrip("]").strip() if "[" in text else ""
                return index, unit
    return None, ""


def read_points_csv(path: Path | str, setup: Sps30Setup, source: str = "file") -> list[Sps30Point]:
    """Read solved design points: this program's table or a Workbench export.

    Droplet diameter in m or mm is converted to um; a mass flow in kg/s to
    L/min; penetration may be given directly or as a trap count with the
    number of injected droplets.
    """
    path = Path(path)
    if not path.is_file():
        raise Sps30StudyError(f"design point file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = [r for r in csv.reader(handle) if any(c.strip() for c in r)]
    rows = [r for r in rows if not r[0].lstrip().startswith("#")]
    if len(rows) < 2:
        raise Sps30StudyError(f"{path.name} has no design point rows")
    header, body = rows[0], rows[1:]
    cols = {f: _find(header, f) for f in _ALIASES}
    missing = [v for v in SPS30_VARIABLES if cols[v][0] is None]
    if missing:
        raise Sps30StudyError(f"{path.name} lacks the column(s) {', '.join(missing)}")
    scale = {"m": 1e6, "mm": 1e3, "um": 1.0, "µm": 1.0, "micron": 1.0, "": 1.0}
    droplet_scale = scale.get(cols["droplet_um"][1], 1.0)
    air, _ = air_properties(setup.ambient_temp_c)

    def num(row, field):
        index = cols[field][0]
        if index is None or index >= len(row) or not row[index].strip():
            return None
        return float(row[index])

    points = []
    for n, row in enumerate(body, start=1):
        try:
            hits = num(row, "sensor_hits")
            injected = num(row, "droplets")
            penetration = num(row, "penetration")
            if penetration is None and hits is not None:
                if injected:
                    penetration = hits / injected
                elif hits == 0:
                    penetration = 0.0
                else:
                    raise ValueError("a trap count needs the number of injected droplets")
            exchange = num(row, "exchange_flow_lpm")
            mass = num(row, "mass_flow")
            if exchange is None and mass is not None:
                exchange = abs(mass) / air * 60000.0
            integral = num(row, "ux_integral")
            if exchange is None and integral is not None:
                # Integral of |u_x| over the chamber plane counts the
                # exchange twice (in and out).
                exchange = 0.5 * abs(integral) * 60000.0
            index = cols["name"][0]
            points.append(
                Sps30Point(
                    name=(row[index].strip() if index is not None and index < len(row) else "") or f"DP{n}",
                    speed_ms=num(row, "speed_ms"),
                    yaw_deg=num(row, "yaw_deg"),
                    droplet_um=num(row, "droplet_um") * droplet_scale,
                    face_velocity_ms=num(row, "face_velocity_ms"),
                    penetration=penetration,
                    exchange_flow_lpm=exchange,
                    sensor_hits=None if hits is None else int(hits),
                    droplets=None if injected is None else int(injected),
                    source=source,
                )
            )
        except (TypeError, ValueError) as error:
            raise Sps30StudyError(f"{path.name}, row {n}: {error}") from error
    return points
