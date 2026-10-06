"""Radiation-shield study: the setup, the uncertain inputs and the results.

A naturally ventilated radiation shield (a louvred box round a thermometer)
is judged by one number: how far the air at the thermometer sits from the
true ambient air, under sun from above and long-wave radiation from the
ground or a hot vehicle roof below. The study follows the reference
methodology (Atmosphere 2026, 17(3), 272, extended to a vehicle roof):

1. a deterministic Design of Experiments over wind speed, solar flux and
   bottom radiation,
2. a response surface fitted to the solved design points,
3. a Monte Carlo analysis run on that surface, to find the worst case and
   the design's reliability.

The design points can be solved three ways: by the instant analytical
model, by SU2 on this computer, or anywhere else (ANSYS Fluent through
Workbench/DesignXplorer) and imported back as a CSV.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from core.models import StrictModel, Vector3, partial_model
from core.units import celsius_to_kelvin

STEFAN_BOLTZMANN = 5.670374419e-8

# The three uncertain inputs, in the order every table and array uses.
VARIABLE_NAMES = ("wind_speed_ms", "solar_flux_w_m2", "bottom_flux_w_m2")
VARIABLE_LABELS = {
    "wind_speed_ms": "Inlet wind speed [m/s]",
    "solar_flux_w_m2": "Top solar radiation [W/m2]",
    "bottom_flux_w_m2": "Bottom radiation [W/m2]",
}


class BottomMode(str, Enum):
    """What the bottom of the domain is."""

    GROUND_FLUX = "ground_flux"
    ROOF_TEMPERATURE = "roof_temperature"


class Distribution(str, Enum):
    """How a Monte Carlo input is drawn inside its range."""

    UNIFORM = "uniform"
    NORMAL = "normal"
    TRIANGULAR = "triangular"


class VariableScale(str, Enum):
    """Axis the design points are spread along."""

    LINEAR = "linear"
    LOG = "log"


class DoeKind(str, Enum):
    """Design of Experiments scheme."""

    CCD = "ccd"
    LHS = "lhs"


class SurrogateKind(str, Enum):
    """Response surface type."""

    QUADRATIC = "quadratic"
    RBF = "rbf"


class Evaluator(str, Enum):
    """Who solves the design points."""

    ANALYTIC = "analytic"
    SU2 = "su2"
    IMPORTED = "imported"


class StudyVariable(StrictModel):
    """One uncertain input: its range and its Monte Carlo distribution."""

    minimum: float = Field(description="Lower bound of the range.")
    maximum: float = Field(description="Upper bound of the range.")
    distribution: Distribution = Field(
        default=Distribution.UNIFORM,
        description=(
            "'uniform' over the range, 'normal' (mean/std, truncated to the "
            "range) or 'triangular' (peak at 'mode')."
        ),
    )
    mean: float | None = Field(
        default=None, description="Normal distribution mean; null = mid-range."
    )
    std: float | None = Field(
        default=None,
        gt=0.0,
        description="Normal distribution standard deviation; null = range / 6.",
    )
    mode: float | None = Field(
        default=None, description="Triangular distribution peak; null = mid-range."
    )
    scale: VariableScale = Field(
        default=VariableScale.LINEAR,
        description=(
            "'log' spreads design points and fits the response surface in "
            "log of the value -- right for wind speed, whose effect falls "
            "off roughly as 1/sqrt(v)."
        ),
    )

    @model_validator(mode="after")
    def _ordered(self) -> "StudyVariable":
        if not self.maximum > self.minimum:
            raise ValueError("maximum must be greater than minimum")
        if self.scale is VariableScale.LOG and self.minimum <= 0.0:
            raise ValueError("a log-scaled variable must stay positive")
        for name in ("mean", "mode"):
            value = getattr(self, name)
            if value is not None and not self.minimum <= value <= self.maximum:
                raise ValueError(f"{name} must lie inside the range")
        return self


def _wind() -> StudyVariable:
    return StudyVariable(minimum=0.5, maximum=5.0, scale=VariableScale.LOG)


def _solar() -> StudyVariable:
    return StudyVariable(minimum=800.0, maximum=1200.0)


def _bottom() -> StudyVariable:
    return StudyVariable(minimum=300.0, maximum=800.0)


class ShieldSetup(StrictModel):
    """Geometry, domain, surfaces and the baseline condition."""

    shield_step_path: str = Field(
        default="",
        description=(
            "STEP file of the shield. Empty uses the built-in 20 cm "
            "multi-plate shield."
        ),
    )
    scale_to_meters: float | None = Field(
        default=None,
        gt=0.0,
        description="CAD unit multiplier; null reads it from the file.",
    )
    shield_size_m: Vector3 = Field(
        default=[0.2, 0.2, 0.2],
        description=(
            "Outer size [x, y, z] of the shield, m (x along the wind, z up). "
            "Sizes the built-in shield; a STEP file's own size is used for "
            "meshing and this only feeds the analytical model."
        ),
    )
    plate_count: int = Field(
        default=6, ge=2, le=20, description="Louvre plates of the built-in shield."
    )
    domain_size_m: Vector3 = Field(
        default=[2.0, 2.0, 1.44],
        description=(
            "Fluid domain [length along the wind, width, height], m. The "
            "shield sits at its centre."
        ),
    )
    thermometer_xyz_m: Vector3 = Field(
        default=[0.0, 0.0, 0.0],
        description=(
            "Monitor point (the thermometer) relative to the centre of the "
            "shield's bounding box, m: x along the wind, z up."
        ),
    )
    ambient_temp_c: float = Field(
        default=25.0, ge=-50.0, le=60.0, description="Inlet air temperature, C."
    )
    wind_speed_ms: float = Field(
        default=1.0, gt=0.0, le=30.0, description="Baseline inlet wind speed, m/s."
    )
    solar_flux_w_m2: float = Field(
        default=1000.0, ge=0.0, le=1500.0,
        description="Baseline direct downward solar radiation, W/m^2.",
    )
    bottom_mode: BottomMode = Field(
        default=BottomMode.GROUND_FLUX,
        description=(
            "'ground_flux': the bottom emits bottom_flux_w_m2 of long-wave "
            "radiation (baseline ground). 'roof_temperature': the bottom is "
            "a dark vehicle roof at a fixed temperature, which also heats the "
            "air flowing over it."
        ),
    )
    bottom_flux_w_m2: float = Field(
        default=300.0, ge=0.0, le=1500.0,
        description="Baseline upward long-wave flux from the bottom, W/m^2.",
    )
    bottom_temperature_k: float = Field(
        default=343.0, gt=200.0, le=450.0,
        description=(
            "Roof temperature in 'roof_temperature' mode, K (343 K = 70 C). "
            "In a study the bottom flux variable sets it through "
            "q = e_roof * sigma * T^4."
        ),
    )
    roof_emissivity: float = Field(
        default=0.95, gt=0.0, le=1.0, description="Long-wave emissivity of the roof."
    )
    sky_longwave_w_m2: float | None = Field(
        default=None, ge=0.0, le=600.0,
        description=(
            "Downward long-wave flux from the sky, W/m^2. Null estimates it "
            "from the ambient temperature (Swinbank); 0 reproduces a model "
            "with the solar flux as the only radiation from above."
        ),
    )
    ground_albedo: float = Field(
        default=0.2, ge=0.0, le=1.0,
        description="Fraction of the solar flux reflected up from the bottom.",
    )
    shield_solar_absorptivity: float = Field(
        default=0.2, ge=0.0, le=1.0,
        description="Solar absorptivity of the shield (white plastic ~0.2).",
    )
    shield_emissivity: float = Field(
        default=0.9, ge=0.0, le=1.0, description="Long-wave emissivity of the shield."
    )
    top_side_solar_absorptivity: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description=(
            "Solar absorptivity of the shield faces that look UP (towards the "
            "sun), e.g. 0.15 for shiny aluminium. Null = shield_solar_absorptivity."
        ),
    )
    top_side_emissivity: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description=(
            "Long-wave emissivity of the faces that look up, e.g. 0.1 for "
            "shiny aluminium. Null = shield_emissivity."
        ),
    )
    bottom_side_solar_absorptivity: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description=(
            "Solar absorptivity of the faces that look DOWN (towards the "
            "ground), e.g. 0.95 for black paint. Null = shield_solar_absorptivity."
        ),
    )
    bottom_side_emissivity: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description=(
            "Long-wave emissivity of the faces that look down, e.g. 0.9 for "
            "black paint. Null = shield_emissivity."
        ),
    )
    optics_orientation: Literal["vertical", "thermometer"] = Field(
        default="vertical",
        description=(
            "How the two-sided optics are assigned. 'vertical': top_side_* are "
            "the faces looking up, bottom_side_* those looking down. "
            "'thermometer': top_side_* are the faces looking AWAY from the "
            "thermometer (the outside of the shield) and bottom_side_* the faces "
            "looking TOWARDS it (the inside of the gaps), e.g. shiny outside "
            "0.15/0.1, black inside 0.95/0.9."
        ),
    )
    shield_conductivity_w_mk: float = Field(
        default=0.2, gt=0.0, le=500.0,
        description="Conductivity of the shield material (Fluent solid zone).",
    )
    ventilation_coefficient: float = Field(
        default=0.35, gt=0.0, le=1.0,
        description=(
            "Analytical model only: air speed inside the shield as a fraction "
            "of the wind speed."
        ),
    )

    def side_optics(self) -> dict[str, tuple[float, float]]:
        """(solar absorptivity, emissivity) of the up- and down-facing faces.

        Faces that look sideways take the mean of the two.
        """
        top = (
            self.shield_solar_absorptivity if self.top_side_solar_absorptivity is None
            else self.top_side_solar_absorptivity,
            self.shield_emissivity if self.top_side_emissivity is None
            else self.top_side_emissivity,
        )
        bottom = (
            self.shield_solar_absorptivity if self.bottom_side_solar_absorptivity is None
            else self.bottom_side_solar_absorptivity,
            self.shield_emissivity if self.bottom_side_emissivity is None
            else self.bottom_side_emissivity,
        )
        side = (0.5 * (top[0] + bottom[0]), 0.5 * (top[1] + bottom[1]))
        return {"top": top, "bottom": bottom, "side": side}

    def two_sided(self) -> bool:
        """Do the up- and down-facing faces differ?"""
        optics = self.side_optics()
        return optics["top"] != optics["bottom"]

    def ambient_temp_k(self) -> float:
        """Inlet air temperature in kelvin."""
        return celsius_to_kelvin(self.ambient_temp_c)

    def sky_flux_w_m2(self) -> float:
        """Downward long-wave flux, explicit or from Swinbank's sky temperature."""
        if self.sky_longwave_w_m2 is not None:
            return self.sky_longwave_w_m2
        sky = 0.0552 * self.ambient_temp_k() ** 1.5
        return STEFAN_BOLTZMANN * sky**4

    def roof_temperature_for(self, bottom_flux_w_m2: float) -> float:
        """Roof temperature emitting a given long-wave flux."""
        return (bottom_flux_w_m2 / (self.roof_emissivity * STEFAN_BOLTZMANN)) ** 0.25

    def roof_flux_for(self, temperature_k: float) -> float:
        """Long-wave flux a roof at this temperature emits."""
        return self.roof_emissivity * STEFAN_BOLTZMANN * temperature_k**4

    def baseline_bottom_flux(self) -> float:
        """The bottom flux of the baseline condition, whichever mode."""
        if self.bottom_mode is BottomMode.ROOF_TEMPERATURE:
            return self.roof_flux_for(self.bottom_temperature_k)
        return self.bottom_flux_w_m2

    @model_validator(mode="after")
    def _shield_fits(self) -> "ShieldSetup":
        for index, (shield, domain) in enumerate(
            zip(self.shield_size_m, self.domain_size_m)
        ):
            if shield <= 0.0:
                raise ValueError("shield dimensions must be positive")
            if domain < 2.0 * shield:
                raise ValueError(
                    f"domain axis {'xyz'[index]} ({domain:g} m) must be at "
                    f"least twice the shield ({shield:g} m)"
                )
        for value, half in zip(self.thermometer_xyz_m, self.shield_size_m):
            if abs(value) > 0.5 * half + 1.0e-9:
                raise ValueError("the thermometer must lie inside the shield")
        return self


class ShieldStudyParams(StrictModel):
    """A full study: the setup, the three inputs and how to explore them."""

    setup: ShieldSetup = Field(default_factory=ShieldSetup)
    wind_speed_ms: StudyVariable = Field(default_factory=_wind)
    solar_flux_w_m2: StudyVariable = Field(default_factory=_solar)
    bottom_flux_w_m2: StudyVariable = Field(default_factory=_bottom)
    doe: DoeKind = Field(
        default=DoeKind.CCD,
        description=(
            "'ccd': face-centred central composite, 15 points for three "
            "inputs (DesignXplorer's default). 'lhs': Latin hypercube with "
            "doe_points points."
        ),
    )
    doe_points: int = Field(
        default=20, ge=6, le=500, description="Points of a Latin hypercube DoE."
    )
    surrogate: SurrogateKind = Field(
        default=SurrogateKind.QUADRATIC,
        description=(
            "'quadratic': full second-order polynomial (smooth, reports its "
            "own fit error). 'rbf': thin-plate radial basis interpolation "
            "through every point (Kriging-like; needs more points)."
        ),
    )
    monte_carlo_samples: int = Field(
        default=10000, ge=100, le=2_000_000,
        description="Random environmental conditions drawn on the response surface.",
    )
    tolerance_k: float = Field(
        default=0.5, gt=0.0, le=20.0,
        description=(
            "Reliability is the probability that |T_monitor - T_inlet| stays "
            "within this, K."
        ),
    )
    seed: int = Field(default=1, ge=0, description="Random seed, for repeatability.")

    def variables(self) -> dict[str, StudyVariable]:
        """The three inputs keyed by name, in the standard order."""
        return {name: getattr(self, name) for name in VARIABLE_NAMES}


class ShieldCfdSettings(StrictModel):
    """How the design points are meshed and solved with SU2."""

    mesh_resolution: str = Field(
        default="coarse",
        pattern="^(coarse|medium|fine)$",
        description="'coarse' ~0.3M, 'medium' ~1M, 'fine' ~3M cells.",
    )
    turbulence_model: str = Field(
        default="SST",
        pattern="^(SST|SA)$",
        description=(
            "SU2 has no standard k-epsilon; SST k-omega is used instead. The "
            "Fluent export uses standard k-epsilon as specified."
        ),
    )
    buoyancy: bool = Field(
        default=True,
        description="Gravity and variable density: natural convection at low wind.",
    )
    radiation_passes: int = Field(
        default=8, ge=1, le=20,
        description=(
            "Most outer passes per design point. Each refreshes the long-wave "
            "emission (and, with solid conduction, the plate temperatures) from "
            "the last air solve; the loop stops as soon as the shield surface "
            "changes by less than 0.05 K between passes."
        ),
    )
    solid_conduction: str = Field(
        default="on", pattern="^(on|off)$",
        description=(
            "'on': conjugate heat transfer -- heat conducts inside the shield "
            "plates (tetrahedral solid mesh, coupled to SU2 pass by pass), so "
            "sun absorbed on the top of a metal plate reaches its other faces. "
            "'off': every surface facet is an independent wall (older, faster, "
            "wrong for metal plates)."
        ),
    )
    iterations_first_pass: int = Field(
        default=3000, ge=50, le=100_000,
        description="Iteration cap of the first pass; SU2 stops earlier once converged.",
    )
    iterations_later_passes: int = Field(
        default=1500, ge=50, le=100_000,
        description=(
            "Iteration cap of each later pass. A pass that hits it leaves the air "
            "temperature half-converged, and the passes then creep instead of settling."
        ),
    )
    radiation_classes: int = Field(
        default=8, ge=2, le=32,
        description="Surface groups by absorbed radiation (one SU2 marker each).",
    )
    rays_per_face: int = Field(
        default=64, ge=8, le=1024,
        description="Rays per surface facet for shading and view factors.",
    )
    mpi_ranks: int = Field(default=4, ge=1, le=256)



class DesignPoint(StrictModel):
    """One deterministic condition and, once solved, its temperature error."""

    name: str
    wind_speed_ms: float
    solar_flux_w_m2: float
    bottom_flux_w_m2: float
    delta_t_k: float | None = Field(
        default=None,
        description="T_monitor - T_inlet, K; null until the point is solved.",
    )
    source: str = Field(default="", description="Who solved it: analytic, su2, file.")

    def inputs(self) -> list[float]:
        """The three inputs in standard order."""
        return [getattr(self, name) for name in VARIABLE_NAMES]


class SurrogateSummary(StrictModel):
    """How well the response surface reproduces the design points."""

    kind: SurrogateKind
    points: int
    r_squared: float = Field(description="Fit through the design points (1 = exact).")
    loo_rmse_k: float = Field(
        description=(
            "Leave-one-out prediction error, K: each point predicted by a "
            "surface fitted without it. The honest accuracy figure."
        )
    )
    coefficients: dict[str, float] = Field(
        default_factory=dict,
        description="Quadratic terms in coded inputs (-1..1); empty for RBF.",
    )


class MonteCarloSummary(StrictModel):
    """Statistics of T_monitor - T_inlet over the random conditions."""

    samples: int
    mean_k: float
    std_k: float
    min_k: float
    max_k: float
    p05_k: float
    p50_k: float
    p95_k: float
    p99_k: float
    abs_p95_k: float = Field(description="95th percentile of |dT|, K.")
    abs_max_k: float = Field(description="Worst |dT| found, K.")
    reliability: float = Field(description="Probability that |dT| <= tolerance.")
    tolerance_k: float
    worst_case: dict[str, float] = Field(
        description="Inputs and dT of the worst sample."
    )
    sensitivities: dict[str, float] = Field(
        description="Spearman rank correlation of dT with each input (-1..1)."
    )
    histogram_counts: list[int]
    histogram_edges_k: list[float]


class ShieldStudyResult(StrictModel):
    """Everything a study produced."""

    study_id: str = ""
    params: ShieldStudyParams
    evaluator: Evaluator
    design_points: list[DesignPoint]
    surrogate: SurrogateSummary
    monte_carlo: MonteCarloSummary
    worst_case_check_k: float | None = Field(
        default=None,
        description=(
            "The worst case re-solved directly (analytic evaluator only); "
            "compare it with monte_carlo.worst_case to see the surface's error "
            "where it matters most."
        ),
    )
    notes: list[str] = Field(default_factory=list)


# Overrides the assistant's tools take: every field optional, so a call
# names only what it changes, and the schema still lists every field.
ShieldSetupOverrides = partial_model(ShieldSetup, "ShieldSetupOverrides")
StudyVariableOverrides = partial_model(StudyVariable, "StudyVariableOverrides")


class ShieldVariablesOverrides(StrictModel):
    """Ranges of the three uncertain inputs (only those being changed)."""

    wind_speed_ms: StudyVariableOverrides | None = None  # type: ignore[valid-type]
    solar_flux_w_m2: StudyVariableOverrides | None = None  # type: ignore[valid-type]
    bottom_flux_w_m2: StudyVariableOverrides | None = None  # type: ignore[valid-type]


class ShieldStudyOverrides(StrictModel):
    """How to explore the inputs (only what is being changed)."""

    doe: DoeKind | None = Field(default=None, description="'ccd' (15 points) or 'lhs'.")
    doe_points: int | None = Field(default=None, description="Points of a Latin hypercube.")
    surrogate: SurrogateKind | None = Field(default=None, description="'quadratic' or 'rbf'.")
    monte_carlo_samples: int | None = None
    tolerance_k: float | None = Field(default=None, description="Reliability threshold, K.")
    seed: int | None = None


ShieldCfdOverrides = partial_model(ShieldCfdSettings, "ShieldCfdOverrides")


class ShieldCondition(StrictModel):
    """One condition to evaluate or to solve as an extra CFD point.

    Missing inputs take the setup's baseline (in roof mode the bottom flux
    of the roof at bottom_temperature_k).
    """

    wind_speed_ms: float | None = Field(default=None, gt=0.0, description="Wind speed, m/s.")
    solar_flux_w_m2: float | None = Field(default=None, ge=0.0, description="Top solar flux, W/m^2.")
    bottom_flux_w_m2: float | None = Field(
        default=None, ge=0.0, description="Bottom long-wave flux, W/m^2 (roof mode: sets the roof temperature)."
    )

    def resolve(self, setup: "ShieldSetup") -> tuple[float, float, float]:
        return (
            self.wind_speed_ms if self.wind_speed_ms is not None else setup.wind_speed_ms,
            self.solar_flux_w_m2 if self.solar_flux_w_m2 is not None else setup.solar_flux_w_m2,
            self.bottom_flux_w_m2 if self.bottom_flux_w_m2 is not None else setup.baseline_bottom_flux(),
        )
