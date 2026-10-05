"""SPS30 housing study: a static-port diffuser and inertial impactor.

A protective housing for an SPS30 particulate sensor on a moving platform.
Air enters through flush slits on both sides (static pressure, not ram
pressure), slows down in a plenum, turns 90 degrees round a baffle and
reaches the sensor face; water droplets are too heavy to make the turn,
hit the baffle and drain through a weep hole.

Three questions, each an output of every design point:

* the **maximum air velocity at the sensor face** (goal below 1 m/s),
* the **droplet penetration** to the sensor face (goal zero),
* the **exchange flow** through the sensor chamber (goal as high as
  possible, so the sensor sees fresh air).

Inputs: platform speed, yaw (crosswind angle) and droplet diameter; the
study is a DoE, response surfaces and a Monte Carlo on them, like the
radiation-shield study.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator

from core.models import StrictModel, Vector3, partial_model

SPS30_VARIABLES = ("speed_ms", "yaw_deg", "droplet_um")
SPS30_LABELS = {
    "speed_ms": "Platform speed [m/s]",
    "yaw_deg": "Yaw / crosswind angle [deg]",
    "droplet_um": "Droplet diameter [um]",
}
SPS30_OUTPUTS = ("face_velocity_ms", "penetration", "exchange_flow_lpm")
SPS30_OUTPUT_LABELS = {
    "face_velocity_ms": "Max velocity at sensor face [m/s]",
    "penetration": "Droplet penetration to sensor face [-]",
    "exchange_flow_lpm": "Chamber exchange flow [L/min]",
}


class Sps30Evaluator(str, Enum):
    ANALYTIC = "analytic"
    SU2 = "su2"
    IMPORTED = "imported"


class Sps30Variable(StrictModel):
    """One uncertain input."""

    minimum: float
    maximum: float
    distribution: str = Field(default="uniform", pattern="^(uniform|normal|triangular)$")
    mean: float | None = None
    std: float | None = Field(default=None, gt=0.0)
    mode: float | None = None
    log: bool = Field(default=False, description="Spread points and sample in log of the value.")

    @model_validator(mode="after")
    def _ordered(self) -> "Sps30Variable":
        if not self.maximum > self.minimum:
            raise ValueError("maximum must be greater than minimum")
        if self.log and self.minimum <= 0.0:
            raise ValueError("a log-scaled variable must stay positive")
        return self


class Sps30Setup(StrictModel):
    """Housing geometry, sensor, fan, domain and the design targets."""

    housing_step_path: str = Field(
        default="",
        description="STEP of the housing (solid with its internal passages). Empty = built-in housing.",
    )
    scale_to_meters: float | None = Field(default=None, gt=0.0)
    sensor_face_center_m: Vector3 = Field(
        default=[0.107, 0.0, 0.03],
        description="Centre of the SPS30 intake face in housing CAD coordinates (m).",
    )
    sensor_face_normal: str = Field(
        default="-x",
        pattern="^[+-][xyz]$",
        description="Direction the sensor face looks (into the chamber), e.g. '-x'.",
    )
    sensor_face_size_m: list[float] = Field(
        default=[0.02, 0.02], min_length=2, max_length=2,
        description="Size of the sensor face (two in-plane dimensions), m.",
    )
    chamber_plane_x_m: float = Field(
        default=0.085,
        description="x of the plane across the sensor chamber the exchange flow is measured on.",
    )
    fan_enabled: bool = Field(default=True, description="Model the SPS30 fan as a small extraction at the face.")
    fan_flow_lpm: float = Field(
        default=0.3, gt=0.0, le=20.0,
        description="SPS30 fan flow, L/min (an estimate; set from a measurement).",
    )
    forward_axis: str = Field(
        default="-x", pattern="^[+-][xyz]$",
        description="Direction the platform travels in housing coordinates; the wind comes from it.",
    )
    domain_multipliers: dict[str, float] = Field(
        default_factory=lambda: {"upstream": 3.0, "downstream": 6.0, "lateral": 3.0, "vertical": 3.0},
        description="Wind-tunnel size in housing lengths.",
    )
    ambient_temp_c: float = Field(default=15.0, ge=-40.0, le=50.0)
    water_density_kg_m3: float = Field(default=998.0, gt=0.0)
    speed_ms: float = Field(default=20.0, gt=0.0, le=80.0, description="Baseline platform speed.")
    yaw_deg: float = Field(default=0.0, ge=-45.0, le=45.0)
    droplet_um: float = Field(default=100.0, gt=0.0, le=5000.0)
    # Lumped-model dimensions (the built-in housing's by default).
    port_area_m2: float = Field(default=6.0e-5, gt=0.0, description="Area of ONE side slit, m^2.")
    port_width_m: float = Field(default=0.003, gt=0.0, description="Slit width (short side), m.")
    plenum_area_m2: float = Field(default=0.064 * 0.074, gt=0.0, description="Plenum cross-section, m^2.")
    baffle_gap_m: float = Field(default=0.015, gt=0.0, description="Gap the air turns through round the baffle, m.")
    chamber_area_m2: float = Field(default=0.064 * 0.074, gt=0.0, description="Sensor chamber cross-section, m^2.")
    chamber_fraction: float = Field(
        default=0.3, gt=0.0, le=1.0,
        description="Analytic model: share of the port cross-flow that detours through the sensor chamber.",
    )
    max_face_velocity_ms: float = Field(default=1.0, gt=0.0, description="Goal: face velocity below this.")
    max_penetration: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description="Goal: penetration at most this (0 = no droplet may reach the face).",
    )
    min_exchange_flow_lpm: float = Field(
        default=0.0, ge=0.0, description="Goal: exchange flow at least this (0 = not a pass/fail criterion).",
    )

    def fan_flow_m3s(self) -> float:
        return self.fan_flow_lpm / 60000.0 if self.fan_enabled else 0.0

    def face_area_m2(self) -> float:
        return self.sensor_face_size_m[0] * self.sensor_face_size_m[1]


def _speed() -> Sps30Variable:
    return Sps30Variable(minimum=5.0, maximum=35.0)


def _yaw() -> Sps30Variable:
    return Sps30Variable(minimum=-20.0, maximum=20.0)


def _droplet() -> Sps30Variable:
    return Sps30Variable(minimum=10.0, maximum=2000.0, log=True)


class Sps30StudyParams(StrictModel):
    setup: Sps30Setup = Field(default_factory=Sps30Setup)
    speed_ms: Sps30Variable = Field(default_factory=_speed)
    yaw_deg: Sps30Variable = Field(default_factory=_yaw)
    droplet_um: Sps30Variable = Field(default_factory=_droplet)
    doe: str = Field(default="ccd", pattern="^(ccd|lhs)$")
    doe_points: int = Field(default=25, ge=6, le=500)
    surrogate: str = Field(default="rbf", pattern="^(quadratic|rbf)$")
    monte_carlo_samples: int = Field(default=10000, ge=100, le=2_000_000)
    seed: int = Field(default=1, ge=0)

    def variables(self) -> dict[str, Sps30Variable]:
        return {name: getattr(self, name) for name in SPS30_VARIABLES}


class Sps30CfdSettings(StrictModel):
    mesh_resolution: str = Field(default="coarse", pattern="^(coarse|medium|fine)$")
    iterations: int = Field(default=2000, ge=50, le=100_000)
    mpi_ranks: int = Field(default=4, ge=1, le=256)
    droplets: int = Field(default=2000, ge=10, le=200_000, description="Droplets tracked per design point.")
    random_walk: bool = Field(default=True, description="Discrete random walk (turbulent dispersion).")
    seed: int = Field(default=1, ge=0)


class Sps30Point(StrictModel):
    name: str
    speed_ms: float
    yaw_deg: float
    droplet_um: float
    face_velocity_ms: float | None = None
    penetration: float | None = None
    exchange_flow_lpm: float | None = None
    sensor_hits: int | None = None
    droplets: int | None = None
    source: str = ""

    def inputs(self) -> list[float]:
        return [self.speed_ms, self.yaw_deg, self.droplet_um]

    def solved(self) -> bool:
        return None not in (self.face_velocity_ms, self.penetration, self.exchange_flow_lpm)


class OutputSummary(StrictModel):
    name: str
    r_squared: float
    loo_rmse: float
    mean: float
    p05: float
    p95: float
    minimum: float
    maximum: float
    worst_case: dict[str, float]
    sensitivities: dict[str, float]
    histogram_counts: list[int]
    histogram_edges: list[float]


class Sps30Result(StrictModel):
    study_id: str = ""
    params: Sps30StudyParams
    evaluator: Sps30Evaluator
    points: list[Sps30Point]
    outputs: dict[str, OutputSummary]
    samples: int
    reliability: float = Field(description="Share of random conditions meeting every goal.")
    reliability_by_goal: dict[str, float]
    first_failure: dict[str, float] | None = Field(
        default=None, description="The failing sample furthest from its goals, if any."
    )
    notes: list[str] = Field(default_factory=list)


# Overrides the assistant's tool takes (every field optional, all listed).
Sps30SetupOverrides = partial_model(Sps30Setup, "Sps30SetupOverrides")
Sps30VariableOverrides = partial_model(Sps30Variable, "Sps30VariableOverrides")


class Sps30VariablesOverrides(StrictModel):
    """Ranges of the three inputs (only those being changed)."""

    speed_ms: Sps30VariableOverrides | None = None  # type: ignore[valid-type]
    yaw_deg: Sps30VariableOverrides | None = None  # type: ignore[valid-type]
    droplet_um: Sps30VariableOverrides | None = None  # type: ignore[valid-type]


class Sps30StudyOverrides(StrictModel):
    """How to explore the inputs (only what is being changed)."""

    doe: str | None = Field(default=None, description="'ccd' (15 points) or 'lhs'.")
    doe_points: int | None = None
    surrogate: str | None = Field(default=None, description="'rbf' or 'quadratic'.")
    monte_carlo_samples: int | None = None
    seed: int | None = None


Sps30CfdOverrides = partial_model(Sps30CfdSettings, "Sps30CfdOverrides")


class Sps30Condition(StrictModel):
    """One condition to evaluate or to solve as an extra CFD point (missing = baseline)."""

    speed_ms: float | None = Field(default=None, gt=0.0, description="Platform speed, m/s.")
    yaw_deg: float | None = Field(default=None, description="Yaw (crosswind angle), deg.")
    droplet_um: float | None = Field(default=None, gt=0.0, description="Droplet diameter, um.")

    def resolve(self, setup: "Sps30Setup") -> tuple[float, float, float]:
        return (
            self.speed_ms if self.speed_ms is not None else setup.speed_ms,
            self.yaw_deg if self.yaw_deg is not None else setup.yaw_deg,
            self.droplet_um if self.droplet_um is not None else setup.droplet_um,
        )
