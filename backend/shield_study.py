"""Radiation-shield study: analytical model, DoE, response surface, Monte Carlo.

The workflow mirrors what ANSYS Workbench does with a Parameter Set,
DesignXplorer and a Six Sigma / Monte Carlo analysis, so a study run here
and one run in Fluent can be compared point for point:

* :func:`design_of_experiments` spreads deterministic design points over the
  three input ranges (face-centred CCD or a Latin hypercube).
* Each point is solved -- by :class:`ShieldAnalyticModel` in microseconds,
  by SU2 (:mod:`backend.shield_cfd`), or elsewhere and read back with
  :func:`read_design_points_csv`.
* :class:`ResponseSurface` interpolates the solved points.
* :func:`monte_carlo` draws thousands of random conditions on that surface
  and reports the spread, the worst case and the reliability.

The analytical model is a screening tool: one lumped shield temperature from
a radiation/convection balance, and the air at the thermometer warmed by the
louvres it flows past. It gets the trends right (error grows with sun and
falls roughly as 1/sqrt(wind)); the CFD points give the numbers.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import numpy as np

from core.atmosphere import CP_AIR, R_SPECIFIC_AIR, isa_state
from core.shield_models import (
    STEFAN_BOLTZMANN,
    VARIABLE_NAMES,
    BottomMode,
    DesignPoint,
    Distribution,
    DoeKind,
    Evaluator,
    MonteCarloSummary,
    ShieldSetup,
    ShieldStudyParams,
    ShieldStudyResult,
    StudyVariable,
    SurrogateKind,
    SurrogateSummary,
    VariableScale,
)

PRANDTL_AIR = 0.71
AIR_CONDUCTIVITY = 0.0262


class ShieldStudyError(RuntimeError):
    """A study could not be set up or completed."""


# ---------------------------------------------------------------------------
# Analytical model
# ---------------------------------------------------------------------------


@dataclass
class ShieldBalance:
    """What the analytical model found for one condition."""

    shield_temp_k: float
    monitor_temp_k: float
    ambient_temp_k: float
    absorbed_w: float
    external_h_w_m2k: float
    internal_h_w_m2k: float
    internal_mass_flow_kg_s: float
    inlet_warming_k: float

    @property
    def delta_t_k(self) -> float:
        """T_monitor - T_inlet, K."""
        return self.monitor_temp_k - self.ambient_temp_k


def _forced_h(speed: float, length: float, density: float, viscosity: float) -> float:
    """Laminar/turbulent flat-plate average coefficient, W/(m^2 K)."""
    if speed <= 0.0:
        return 0.0
    reynolds = density * speed * length / viscosity
    if reynolds < 5.0e5:
        nusselt = 0.664 * math.sqrt(reynolds) * PRANDTL_AIR ** (1.0 / 3.0)
    else:
        nusselt = (0.037 * reynolds**0.8 - 871.0) * PRANDTL_AIR ** (1.0 / 3.0)
    return nusselt * AIR_CONDUCTIVITY / length


def _natural_h(delta_t: float) -> float:
    """Simplified natural convection in air, W/(m^2 K)."""
    return 1.31 * abs(delta_t) ** (1.0 / 3.0)


def _combined_h(forced: float, natural: float) -> float:
    """Mixed convection, Churchill-style cubic blend."""
    return (forced**3 + natural**3) ** (1.0 / 3.0)


class ShieldAnalyticModel:
    """Lumped energy balance of a louvred shield round a thermometer.

    1. **Shield.** Absorbed sun (top, and reflected from below), long-wave from
       the sky and the bottom, minus its own emission, is carried away by the
       wind outside and by the air flowing between the louvres.
    2. **Air at the thermometer.** Enters at ambient (or slightly warmed by a
       hot roof), and picks up heat from the louvres on its way to the
       centre: ``dT = (T_shield - T_air)(1 - exp(-NTU/2))``.
    """

    def __init__(self, setup: ShieldSetup) -> None:
        self.setup = setup
        length, width, height = setup.shield_size_m
        self.top_area = length * width
        self.bottom_area = length * width
        self.side_area = 2.0 * (length + width) * height
        self.external_area = self.top_area + self.bottom_area + self.side_area
        # Annular louvres, both faces wetted, about 60 % of the plan area.
        self.internal_area = 2.0 * setup.plate_count * 0.6 * length * width
        # Air enters through roughly half the side face between the plates.
        self.flow_area = 0.5 * width * height
        self.length = length
        state = isa_state(0.0)
        self.pressure = state.pressure_pa
        self.viscosity = state.viscosity_pa_s

    def solve(
        self, wind_speed_ms: float, solar_flux_w_m2: float, bottom_flux_w_m2: float
    ) -> ShieldBalance:
        """Balance the shield for one condition."""
        setup = self.setup
        ambient = setup.ambient_temp_k()
        density = self.pressure / (R_SPECIFIC_AIR * ambient)
        alpha = setup.shield_solar_absorptivity
        emissivity = setup.shield_emissivity
        sky = setup.sky_flux_w_m2()
        reflected = setup.ground_albedo * solar_flux_w_m2

        absorbed = (
            self.top_area * (alpha * solar_flux_w_m2 + emissivity * sky)
            + self.bottom_area * (alpha * reflected + emissivity * bottom_flux_w_m2)
            + self.side_area
            * 0.5
            * (emissivity * (sky + bottom_flux_w_m2) + alpha * reflected)
        )

        forced_out = _forced_h(wind_speed_ms, self.length, density, self.viscosity)
        inner_speed = setup.ventilation_coefficient * wind_speed_ms
        forced_in = _forced_h(inner_speed, self.length, density, self.viscosity)
        mass_flow = density * inner_speed * self.flow_area
        inlet_warming = self._inlet_warming(wind_speed_ms, density, bottom_flux_w_m2)
        air_in = ambient + inlet_warming

        def residual(shield: float) -> tuple[float, float, float, float]:
            natural = _natural_h(shield - air_in)
            h_out = _combined_h(forced_out, natural)
            h_in = _combined_h(forced_in, natural)
            ntu = h_in * self.internal_area / max(mass_flow * CP_AIR, 1.0e-9)
            internal = mass_flow * CP_AIR * (shield - air_in) * (1.0 - math.exp(-ntu))
            emitted = emissivity * STEFAN_BOLTZMANN * shield**4 * self.external_area
            balance = (
                absorbed
                - emitted
                - h_out * self.external_area * (shield - air_in)
                - internal
            )
            return balance, h_out, h_in, ntu

        low, high = ambient - 80.0, ambient + 150.0
        for _ in range(100):
            middle = 0.5 * (low + high)
            if residual(middle)[0] > 0.0:
                low = middle
            else:
                high = middle
        shield = 0.5 * (low + high)
        _, h_out, h_in, ntu = residual(shield)
        monitor = air_in + (shield - air_in) * (1.0 - math.exp(-0.5 * ntu))
        return ShieldBalance(
            shield_temp_k=shield,
            monitor_temp_k=monitor,
            ambient_temp_k=ambient,
            absorbed_w=absorbed,
            external_h_w_m2k=h_out,
            internal_h_w_m2k=h_in,
            internal_mass_flow_kg_s=mass_flow,
            inlet_warming_k=inlet_warming,
        )

    def delta_t(
        self, wind_speed_ms: float, solar_flux_w_m2: float, bottom_flux_w_m2: float
    ) -> float:
        """T_monitor - T_inlet for one condition, K."""
        return self.solve(wind_speed_ms, solar_flux_w_m2, bottom_flux_w_m2).delta_t_k

    def _inlet_warming(self, wind: float, density: float, bottom_flux: float) -> float:
        """Warming of the air reaching the shield by a hot roof below it.

        Only in roof mode: the thermal boundary layer grown over the roof from
        the inlet to the shield, against the height of the shield's base.
        """
        setup = self.setup
        if setup.bottom_mode is not BottomMode.ROOF_TEMPERATURE:
            return 0.0
        roof = setup.roof_temperature_for(bottom_flux)
        run = 0.5 * setup.domain_size_m[0]
        reynolds = max(density * wind * run / self.viscosity, 1.0)
        layer = 5.0 * run / math.sqrt(reynolds) / PRANDTL_AIR ** (1.0 / 3.0)
        clearance = 0.5 * (setup.domain_size_m[2] - setup.shield_size_m[2])
        return (roof - setup.ambient_temp_k()) * math.exp(-clearance / max(layer, 1e-6))


# ---------------------------------------------------------------------------
# Coding: physical values <-> the unit cube the DoE and surface work in
# ---------------------------------------------------------------------------


def _to_axis(variable: StudyVariable, values: np.ndarray) -> np.ndarray:
    return np.log(values) if variable.scale is VariableScale.LOG else values


def _from_axis(variable: StudyVariable, values: np.ndarray) -> np.ndarray:
    return np.exp(values) if variable.scale is VariableScale.LOG else values


def encode(params: ShieldStudyParams, physical: np.ndarray) -> np.ndarray:
    """Map ``(n, 3)`` physical inputs to coded coordinates in [-1, 1]."""
    physical = np.atleast_2d(np.asarray(physical, dtype=float))
    coded = np.empty_like(physical)
    for column, variable in enumerate(params.variables().values()):
        low = _to_axis(variable, np.array(variable.minimum))
        high = _to_axis(variable, np.array(variable.maximum))
        coded[:, column] = 2.0 * (_to_axis(variable, physical[:, column]) - low) / (
            high - low
        ) - 1.0
    return coded


def decode(params: ShieldStudyParams, coded: np.ndarray) -> np.ndarray:
    """Map coded coordinates in [-1, 1] back to physical inputs."""
    coded = np.atleast_2d(np.asarray(coded, dtype=float))
    physical = np.empty_like(coded)
    for column, variable in enumerate(params.variables().values()):
        low = _to_axis(variable, np.array(variable.minimum))
        high = _to_axis(variable, np.array(variable.maximum))
        axis = low + 0.5 * (coded[:, column] + 1.0) * (high - low)
        physical[:, column] = _from_axis(variable, axis)
    return physical


# ---------------------------------------------------------------------------
# Design of Experiments
# ---------------------------------------------------------------------------


def _ccd_coded() -> np.ndarray:
    """Face-centred central composite design for three factors (15 points)."""
    corners = [[a, b, c] for a in (-1, 1) for b in (-1, 1) for c in (-1, 1)]
    faces = []
    for axis in range(3):
        for sign in (-1, 1):
            point = [0, 0, 0]
            point[axis] = sign
            faces.append(point)
    return np.array([[0, 0, 0], *faces, *corners], dtype=float)


def design_of_experiments(params: ShieldStudyParams) -> list[DesignPoint]:
    """The deterministic design points to solve, unsolved."""
    if params.doe is DoeKind.CCD:
        coded = _ccd_coded()
    else:
        from scipy.stats import qmc

        sampler = qmc.LatinHypercube(d=3, optimization="random-cd", seed=params.seed)
        coded = 2.0 * sampler.random(params.doe_points) - 1.0
        # Always include the centre, the baseline everything is judged from.
        coded = np.vstack([np.zeros(3), coded])
    physical = decode(params, coded)
    return [
        DesignPoint(
            name=f"DP{index}",
            wind_speed_ms=round(float(row[0]), 6),
            solar_flux_w_m2=round(float(row[1]), 6),
            bottom_flux_w_m2=round(float(row[2]), 6),
        )
        for index, row in enumerate(physical)
    ]


# ---------------------------------------------------------------------------
# Response surface
# ---------------------------------------------------------------------------

_TERM_NAMES = (
    "1", "w", "s", "b", "w^2", "s^2", "b^2", "w*s", "w*b", "s*b",
)


def _quadratic_features(coded: np.ndarray) -> np.ndarray:
    w, s, b = coded[:, 0], coded[:, 1], coded[:, 2]
    return np.column_stack(
        [np.ones(len(coded)), w, s, b, w * w, s * s, b * b, w * s, w * b, s * b]
    )


class ResponseSurface:
    """A surrogate for dT over the three inputs, fitted to design points."""

    def __init__(
        self,
        params: ShieldStudyParams,
        points: Sequence[DesignPoint],
        kind: SurrogateKind | None = None,
    ) -> None:
        self.params = params
        self.kind = SurrogateKind(kind or params.surrogate)
        solved = [point for point in points if point.delta_t_k is not None]
        self.x = encode(params, np.array([point.inputs() for point in solved]))
        self.y = np.array([point.delta_t_k for point in solved], dtype=float)
        minimum = 10 if self.kind is SurrogateKind.QUADRATIC else 5
        if len(self.y) < minimum:
            raise ShieldStudyError(
                f"a {self.kind.value} response surface needs at least {minimum} "
                f"solved design points; {len(self.y)} are solved"
            )
        self._fit()

    def _fit(self) -> None:
        if self.kind is SurrogateKind.QUADRATIC:
            features = _quadratic_features(self.x)
            self.coefficients, *_ = np.linalg.lstsq(features, self.y, rcond=None)
            self._rbf = None
        else:
            from scipy.interpolate import RBFInterpolator

            self._rbf = RBFInterpolator(
                self.x, self.y, kernel="thin_plate_spline", degree=1
            )
            self.coefficients = None

    def predict_coded(self, coded: np.ndarray) -> np.ndarray:
        """dT at coded inputs."""
        coded = np.atleast_2d(coded)
        if self._rbf is not None:
            return self._rbf(coded)
        return _quadratic_features(coded) @ self.coefficients

    def predict(self, physical: np.ndarray) -> np.ndarray:
        """dT at physical inputs ``(n, 3)``."""
        return self.predict_coded(encode(self.params, physical))

    def summary(self) -> SurrogateSummary:
        """Fit quality, including the leave-one-out error."""
        fitted = self.predict_coded(self.x)
        total = float(np.sum((self.y - self.y.mean()) ** 2))
        r_squared = 1.0 - float(np.sum((self.y - fitted) ** 2)) / total if total > 0 else 1.0
        return SurrogateSummary(
            kind=self.kind,
            points=len(self.y),
            r_squared=r_squared,
            loo_rmse_k=self._leave_one_out(),
            coefficients=(
                {name: float(c) for name, c in zip(_TERM_NAMES, self.coefficients)}
                if self.coefficients is not None
                else {}
            ),
        )

    def _leave_one_out(self) -> float:
        if self.kind is SurrogateKind.QUADRATIC:
            features = _quadratic_features(self.x)
            hat = features @ np.linalg.pinv(features)
            leverage = np.clip(np.diag(hat), 0.0, 1.0 - 1.0e-9)
            residual = self.y - features @ self.coefficients
            press = residual / (1.0 - leverage)
            # A saturated fit (as many points as terms) has no spare point
            # to leave out; say so with NaN rather than a flattering zero.
            if len(self.y) <= features.shape[1]:
                return math.nan
            return float(np.sqrt(np.mean(press**2)))
        from scipy.interpolate import RBFInterpolator

        errors = []
        for index in range(len(self.y)):
            keep = np.arange(len(self.y)) != index
            try:
                model = RBFInterpolator(
                    self.x[keep], self.y[keep], kernel="thin_plate_spline", degree=1
                )
            except Exception:  # noqa: BLE001 - too few points left
                return math.nan
            errors.append(float(model(self.x[index : index + 1])[0] - self.y[index]))
        return float(np.sqrt(np.mean(np.square(errors))))


# ---------------------------------------------------------------------------
# Monte Carlo on the response surface
# ---------------------------------------------------------------------------


def sample_inputs(
    params: ShieldStudyParams, count: int, rng: np.random.Generator
) -> np.ndarray:
    """Random physical inputs ``(count, 3)`` from each variable's distribution."""
    columns = []
    for variable in params.variables().values():
        low, high = variable.minimum, variable.maximum
        middle = 0.5 * (low + high)
        if variable.distribution is Distribution.UNIFORM:
            values = rng.uniform(low, high, count)
        elif variable.distribution is Distribution.TRIANGULAR:
            peak = variable.mode if variable.mode is not None else middle
            values = rng.triangular(low, peak, high, count)
        else:
            from scipy.stats import truncnorm

            mean = variable.mean if variable.mean is not None else middle
            std = variable.std if variable.std is not None else (high - low) / 6.0
            values = truncnorm.rvs(
                (low - mean) / std, (high - mean) / std, loc=mean, scale=std,
                size=count, random_state=rng,
            )
        columns.append(values)
    return np.column_stack(columns)


def monte_carlo(
    params: ShieldStudyParams, surface: ResponseSurface
) -> tuple[MonteCarloSummary, np.ndarray]:
    """Draw random conditions on the surface. Returns the summary and the inputs
    of the worst sample."""
    from scipy.stats import spearmanr

    rng = np.random.default_rng(params.seed)
    inputs = sample_inputs(params, params.monte_carlo_samples, rng)
    delta = surface.predict(inputs)
    magnitude = np.abs(delta)
    worst = int(np.argmax(magnitude))

    sensitivities = {}
    for column, name in enumerate(VARIABLE_NAMES):
        rho = spearmanr(inputs[:, column], delta).statistic
        sensitivities[name] = float(0.0 if np.isnan(rho) else rho)

    counts, edges = np.histogram(delta, bins=40)
    percentile = lambda q: float(np.percentile(delta, q))  # noqa: E731
    summary = MonteCarloSummary(
        samples=len(delta),
        mean_k=float(delta.mean()),
        std_k=float(delta.std(ddof=1)),
        min_k=float(delta.min()),
        max_k=float(delta.max()),
        p05_k=percentile(5),
        p50_k=percentile(50),
        p95_k=percentile(95),
        p99_k=percentile(99),
        abs_p95_k=float(np.percentile(magnitude, 95)),
        abs_max_k=float(magnitude[worst]),
        reliability=float(np.mean(magnitude <= params.tolerance_k)),
        tolerance_k=params.tolerance_k,
        worst_case={
            **{name: float(inputs[worst, column]) for column, name in enumerate(VARIABLE_NAMES)},
            "delta_t_k": float(delta[worst]),
        },
        sensitivities=sensitivities,
        histogram_counts=[int(c) for c in counts],
        histogram_edges_k=[float(e) for e in edges],
    )
    return summary, inputs[worst]


# ---------------------------------------------------------------------------
# A whole study
# ---------------------------------------------------------------------------

PointSolver = Callable[[DesignPoint], float]


def analytic_solver(setup: ShieldSetup) -> PointSolver:
    """Solve design points with the analytical model."""
    model = ShieldAnalyticModel(setup)
    return lambda point: model.delta_t(*point.inputs())


def solve_points(
    points: Iterable[DesignPoint], solver: PointSolver, source: str
) -> list[DesignPoint]:
    """Solve every unsolved point; solved ones are kept as they are."""
    solved = []
    for point in points:
        if point.delta_t_k is None:
            point = point.model_copy(
                update={"delta_t_k": float(solver(point)), "source": source}
            )
        solved.append(point)
    return solved


def analyse(
    params: ShieldStudyParams,
    points: Sequence[DesignPoint],
    evaluator: Evaluator,
    check: PointSolver | None = None,
) -> ShieldStudyResult:
    """Fit the response surface to solved points and run the Monte Carlo."""
    surface = ResponseSurface(params, points)
    summary = surface.summary()
    mc, worst_inputs = monte_carlo(params, surface)
    notes = []
    checked = None
    if check is not None:
        worst_point = DesignPoint(
            name="worst",
            wind_speed_ms=float(worst_inputs[0]),
            solar_flux_w_m2=float(worst_inputs[1]),
            bottom_flux_w_m2=float(worst_inputs[2]),
        )
        checked = float(check(worst_point))
    if not math.isnan(summary.loo_rmse_k) and summary.loo_rmse_k > 0.25 * max(
        mc.abs_max_k, 1.0e-9
    ):
        notes.append(
            f"The response surface predicts left-out points only to within "
            f"{summary.loo_rmse_k:.3f} K, a large share of the spread; add "
            "design points (a Latin hypercube with more points) or try the "
            "other surface type before trusting the tails."
        )
    if params.setup.bottom_mode is BottomMode.ROOF_TEMPERATURE:
        notes.append(
            "Roof mode: each bottom flux is turned into a roof temperature "
            "through q = e_roof * sigma * T^4 "
            f"(e_roof = {params.setup.roof_emissivity:g})."
        )
    if evaluator is Evaluator.ANALYTIC:
        notes.append(
            "Solved with the analytical screening model, which balances one "
            "lumped shield temperature; run the design points with SU2 or "
            "Fluent for design-grade numbers."
        )
    return ShieldStudyResult(
        params=params,
        evaluator=evaluator,
        design_points=list(points),
        surrogate=summary,
        monte_carlo=mc,
        worst_case_check_k=checked,
        notes=notes,
    )


def run_analytic_study(params: ShieldStudyParams) -> ShieldStudyResult:
    """DoE, analytical solve, response surface and Monte Carlo in one go."""
    solver = analytic_solver(params.setup)
    points = solve_points(design_of_experiments(params), solver, "analytic")
    return analyse(params, points, Evaluator.ANALYTIC, check=solver)


# ---------------------------------------------------------------------------
# Design-point tables (for Fluent / DesignXplorer and back)
# ---------------------------------------------------------------------------

CSV_COLUMNS = (
    "name",
    "wind_speed_ms",
    "solar_flux_w_m2",
    "bottom_flux_w_m2",
    "bottom_temperature_k",
    "delta_t_k",
)

# Names a column may carry in a Fluent / DesignXplorer export, in lower case.
_ALIASES = {
    "wind_speed_ms": ("wind_speed_ms", "inlet_velocity", "wind_speed", "velocity"),
    "solar_flux_w_m2": ("solar_flux_w_m2", "solar_flux", "top_flux", "solar"),
    "bottom_flux_w_m2": ("bottom_flux_w_m2", "bottom_flux", "ground_flux", "bottom"),
    "delta_t_k": ("delta_t_k", "delta_t", "dt", "temperature_delta"),
    "monitor_temp_k": ("monitor_temp_k", "monitor_temperature", "t_monitor"),
    "name": ("name", "design point", "design_point", "dp"),
}


def write_design_points_csv(
    path: Path | str, setup: ShieldSetup, points: Sequence[DesignPoint]
) -> Path:
    """Write the design points, with the roof temperature each one implies."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for point in points:
            writer.writerow(
                [
                    point.name,
                    f"{point.wind_speed_ms:.6g}",
                    f"{point.solar_flux_w_m2:.6g}",
                    f"{point.bottom_flux_w_m2:.6g}",
                    f"{setup.roof_temperature_for(point.bottom_flux_w_m2):.3f}",
                    "" if point.delta_t_k is None else f"{point.delta_t_k:.6g}",
                ]
            )
    return path


def _column(header: list[str], field: str) -> int | None:
    lowered = [cell.strip().lower() for cell in header]
    for alias in _ALIASES[field]:
        for index, cell in enumerate(lowered):
            # DesignXplorer writes "P1 - inlet_velocity [m s^-1]".
            name = cell.split(" - ", 1)[-1].split("[", 1)[0].strip()
            if name == alias:
                return index
    return None


def read_design_points_csv(
    path: Path | str, setup: ShieldSetup, source: str = "file"
) -> list[DesignPoint]:
    """Read solved design points back, from this program or a Fluent export.

    Each row needs the three inputs and either ``delta_t_k`` or the monitor
    temperature (the inlet temperature is then subtracted). Rows without a
    result are kept unsolved.
    """
    path = Path(path)
    if not path.is_file():
        raise ShieldStudyError(f"design point file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = [row for row in csv.reader(handle) if any(cell.strip() for cell in row)]
    # DesignXplorer puts '#' comment lines above the header.
    rows = [row for row in rows if not row[0].lstrip().startswith("#")]
    if len(rows) < 2:
        raise ShieldStudyError(f"{path.name} has no design point rows")
    header, body = rows[0], rows[1:]
    columns = {field: _column(header, field) for field in _ALIASES}
    missing = [name for name in VARIABLE_NAMES if columns[name] is None]
    if missing:
        raise ShieldStudyError(
            f"{path.name} lacks the column(s) {', '.join(missing)}; expected "
            f"{', '.join(CSV_COLUMNS)}"
        )
    if columns["delta_t_k"] is None and columns["monitor_temp_k"] is None:
        raise ShieldStudyError(
            f"{path.name} has neither a delta_t_k nor a monitor_temp_k column"
        )

    def number(row: list[str], field: str) -> float | None:
        index = columns[field]
        if index is None or index >= len(row) or not row[index].strip():
            return None
        return float(row[index])

    points = []
    for number_in_file, row in enumerate(body, start=1):
        try:
            delta = number(row, "delta_t_k")
            if delta is None:
                monitor = number(row, "monitor_temp_k")
                delta = None if monitor is None else monitor - setup.ambient_temp_k()
            name_index = columns["name"]
            name = (
                row[name_index].strip()
                if name_index is not None and name_index < len(row)
                else f"DP{number_in_file}"
            )
            points.append(
                DesignPoint(
                    name=name or f"DP{number_in_file}",
                    wind_speed_ms=number(row, "wind_speed_ms"),
                    solar_flux_w_m2=number(row, "solar_flux_w_m2"),
                    bottom_flux_w_m2=number(row, "bottom_flux_w_m2"),
                    delta_t_k=delta,
                    source=source if delta is not None else "",
                )
            )
        except (TypeError, ValueError) as error:
            raise ShieldStudyError(
                f"{path.name}, row {number_in_file}: {error}"
            ) from error
    return points
