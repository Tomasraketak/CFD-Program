"""Typed parameter core for AeroThermalStudio.

This module is the single source of truth for every operator-facing setting in
the platform. The GUI builds its widgets from these models, the MCP server
derives its tool schemas from them, and the SU2 configuration writers read
them. A field added here therefore appears in all three surfaces at once,
which is what makes the "every setting controllable from both the GUI and an
AI agent" requirement tractable without duplicated definitions.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Annotated, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.atmosphere import (
    ALTITUDE_MAX_M,
    ALTITUDE_MIN_M,
    AtmosphereState,
    isa_state,
    state_from_pressure_temperature,
)
from core.frames import Frame, to_rocket, to_solver
from core.units import (
    MACH_SUPPORTED_MAX,
    YPLUS_WALL_FUNCTION_MAX,
    YPLUS_WALL_FUNCTION_MIN,
    celsius_to_kelvin,
    mach_from_speed,
    speed_from_mach,
)

Vector3 = Annotated[list[float], Field(min_length=3, max_length=3)]

# Angle-of-attack / sideslip envelope mandated by the specification.
ANGLE_LIMIT_DEG = 20.0


class StrictModel(BaseModel):
    """Base model rejecting unknown fields so agent typos fail loudly."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------
# Geometry and orientation
# --------------------------------------------------------------------------


class AxisDirection(str, Enum):
    """Named principal axis directions for the nose/forward vector."""

    PLUS_X = "+X"
    MINUS_X = "-X"
    PLUS_Y = "+Y"
    MINUS_Y = "-Y"
    PLUS_Z = "+Z"
    MINUS_Z = "-Z"

    def to_vector(self) -> tuple[float, float, float]:
        """Return the unit vector this named direction denotes."""
        return {
            AxisDirection.PLUS_X: (1.0, 0.0, 0.0),
            AxisDirection.MINUS_X: (-1.0, 0.0, 0.0),
            AxisDirection.PLUS_Y: (0.0, 1.0, 0.0),
            AxisDirection.MINUS_Y: (0.0, -1.0, 0.0),
            AxisDirection.PLUS_Z: (0.0, 0.0, 1.0),
            AxisDirection.MINUS_Z: (0.0, 0.0, -1.0),
        }[self]


def normalize_vector(vector: Sequence[float]) -> tuple[float, float, float]:
    """Normalise a 3-vector, raising if it is degenerate."""
    x, y, z = (float(component) for component in vector)
    magnitude = math.sqrt(x * x + y * y + z * z)
    if magnitude < 1.0e-12:
        raise ValueError("vector has (near) zero magnitude and cannot be normalised")
    return (x / magnitude, y / magnitude, z / magnitude)


class GeometryParams(StrictModel):
    """CAD ingestion, healing and alignment settings."""

    step_file_path: str = Field(
        description="Absolute path to the .step/.stp CAD file to import."
    )
    nose_direction: AxisDirection | None = Field(
        default=AxisDirection.PLUS_X,
        description=(
            "Principal axis the body runs along FROM THE NOSE TOWARDS THE "
            "TAIL, in the CAD file's own coordinate system. This direction "
            "is rotated onto +X, which places the nose at the upstream end "
            "of the tunnel: a model drawn nose-up along +Y therefore takes "
            "'-Y', not '+Y'. Set to null and supply 'nose_vector' to use an "
            "arbitrary direction instead."
        ),
    )
    nose_vector: Vector3 | None = Field(
        default=None,
        description=(
            "Arbitrary nose-to-tail direction [nx, ny, nz] in CAD "
            "coordinates, with the same sense as 'nose_direction'. "
            "Normalised automatically. Takes precedence over it."
        ),
    )
    reference_origin: Vector3 = Field(
        default=[0.0, 0.0, 0.0],
        description=(
            "Point [x0, y0, z0] in CAD coordinates translated to the wind-tunnel "
            "origin before meshing. Typically the nose tip or the CG."
        ),
    )
    heal_geometry: bool = Field(
        default=True,
        description=(
            "Run OpenCASCADE shape healing on import: sew split faces, fix "
            "small/degenerate edges and report non-manifold topology."
        ),
    )
    heal_tolerance_m: float = Field(
        default=1.0e-6,
        gt=0.0,
        description="Sewing/healing tolerance in metres.",
    )
    scale_to_meters: float = Field(
        default=1.0,
        gt=0.0,
        description=(
            "Multiplier applied to imported CAD units to obtain metres. Use "
            "0.001 for models authored in millimetres."
        ),
    )

    @field_validator("nose_vector")
    @classmethod
    def _validate_nose_vector(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return None
        return list(normalize_vector(value))

    @model_validator(mode="after")
    def _require_a_direction(self) -> "GeometryParams":
        if self.nose_vector is None and self.nose_direction is None:
            raise ValueError(
                "specify either 'nose_direction' (named axis) or 'nose_vector'"
            )
        return self

    def resolved_nose_vector(self) -> tuple[float, float, float]:
        """The effective unit nose vector, preferring the explicit vector."""
        if self.nose_vector is not None:
            return normalize_vector(self.nose_vector)
        assert self.nose_direction is not None  # guaranteed by the validator
        return self.nose_direction.to_vector()


# --------------------------------------------------------------------------
# Farfield domain
# --------------------------------------------------------------------------


class DomainShape(str, Enum):
    """Shape of the farfield envelope surrounding the body."""

    CYLINDER = "cylinder"
    BOX = "box"


class DomainParams(StrictModel):
    """Farfield envelope sizing, expressed as multiples of the body length."""

    shape: DomainShape = Field(
        default=DomainShape.CYLINDER,
        description=(
            "'cylinder' for axisymmetric rocket tunnels, 'box' for rectangular "
            "enclosures such as the sensor-housing microclimate case."
        ),
    )
    upstream_multiplier: float = Field(
        default=5.0,
        ge=1.0,
        le=50.0,
        description="Nose-to-inlet distance as a multiple of L_ref.",
    )
    downstream_multiplier: float = Field(
        default=10.0,
        ge=1.0,
        le=50.0,
        description="Base-to-outlet distance as a multiple of L_ref.",
    )
    radial_multiplier: float = Field(
        default=5.0,
        ge=1.0,
        le=50.0,
        description=(
            "Lateral/radial extent as a multiple of L_ref, measured from the "
            "body axis to the farfield boundary."
        ),
    )
    use_symmetry_plane: bool = Field(
        default=False,
        description=(
            "Mesh only the y >= 0 half-domain with a SYMMETRY boundary. Halves "
            "the cell count but is only valid at zero sideslip."
        ),
    )


# --------------------------------------------------------------------------
# Meshing
# --------------------------------------------------------------------------


class MeshResolution(str, Enum):
    """Named mesh density presets mapped to target cell-count bands."""

    COARSE = "coarse"
    MEDIUM = "medium"
    FINE = "fine"


class SimulationTrack(str, Enum):
    """Which of the two physics tracks a run belongs to."""

    AERODYNAMIC = "aerodynamic"
    THERMAL = "thermal"


# Cell-count bands guaranteeing 3-8 min runtimes on 6 cores / 16 GB.
CELL_COUNT_BANDS: dict[tuple[SimulationTrack, MeshResolution], tuple[int, int]] = {
    (SimulationTrack.AERODYNAMIC, MeshResolution.COARSE): (250_000, 400_000),
    (SimulationTrack.AERODYNAMIC, MeshResolution.MEDIUM): (400_000, 600_000),
    (SimulationTrack.AERODYNAMIC, MeshResolution.FINE): (600_000, 750_000),
    (SimulationTrack.THERMAL, MeshResolution.COARSE): (150_000, 230_000),
    (SimulationTrack.THERMAL, MeshResolution.MEDIUM): (230_000, 320_000),
    (SimulationTrack.THERMAL, MeshResolution.FINE): (320_000, 400_000),
}


def cell_count_band(
    track: SimulationTrack, resolution: MeshResolution
) -> tuple[int, int]:
    """Target (min, max) cell count for a track/resolution combination."""
    return CELL_COUNT_BANDS[(track, resolution)]


class MeshParams(StrictModel):
    """Mesh sizing fields, boundary-layer stack and cell-count targeting."""

    resolution: MeshResolution = Field(
        default=MeshResolution.MEDIUM,
        description=(
            "Mesh density preset. Selects the target cell-count band the "
            "pipeline bisects towards: 250k-750k for aerodynamic runs and "
            "150k-400k for thermal runs."
        ),
    )
    track: SimulationTrack = Field(
        default=SimulationTrack.AERODYNAMIC,
        description="Physics track, which selects the cell-count band.",
    )
    boundary_layers: int = Field(
        default=7,
        ge=5,
        le=8,
        description="Number of prism layers extruded from wall surfaces.",
    )
    boundary_layer_growth: float = Field(
        default=1.25,
        ge=1.05,
        le=1.6,
        description="Geometric growth ratio between successive prism layers.",
    )
    target_yplus: float = Field(
        default=45.0,
        ge=YPLUS_WALL_FUNCTION_MIN,
        le=YPLUS_WALL_FUNCTION_MAX,
        description=(
            "y+ at the first cell centroid. The 30-60 band keeps wall functions "
            "valid while holding the cell count down."
        ),
    )
    curvature_points_per_2pi: float = Field(
        default=32.0,
        ge=8.0,
        le=180.0,
        description=(
            "Gmsh curvature-adaptive sizing: nodes per full turn of surface "
            "curvature. Higher values resolve nose radii and fin leading edges "
            "more finely."
        ),
    )
    leading_edge_refinement: float = Field(
        default=0.25,
        gt=0.0,
        le=1.0,
        description=(
            "Cell size at leading/trailing edges as a fraction of the nominal "
            "surface size. Lower values sharpen knife-edge fin resolution."
        ),
    )
    farfield_size_multiplier: float = Field(
        default=40.0,
        ge=2.0,
        le=500.0,
        description=(
            "Farfield cell size as a multiple of the nominal surface size, "
            "controlling how fast the mesh coarsens away from the body."
        ),
    )
    wake_refinement_length: float = Field(
        default=2.0,
        ge=0.0,
        le=20.0,
        description=(
            "Length of the refined wake/plume region downstream of the base, "
            "in multiples of L_ref. Zero disables wake refinement."
        ),
    )
    max_targeting_iterations: int = Field(
        default=4,
        ge=0,
        le=8,
        description=(
            "Maximum remesh attempts used to bisect the characteristic size "
            "until the cell count lands inside the target band."
        ),
    )
    optimize_netgen: bool = Field(
        default=True,
        description="Run Gmsh's Netgen optimiser to improve tetrahedral quality.",
    )

    def target_band(self) -> tuple[int, int]:
        """Target (min, max) cell count for this mesh configuration."""
        return cell_count_band(self.track, self.resolution)


# --------------------------------------------------------------------------
# Flow conditions
# --------------------------------------------------------------------------


class VelocityType(str, Enum):
    """How the operator specified the freestream speed."""

    MACH = "mach"
    TAS = "tas"


class FlowParams(StrictModel):
    """Freestream conditions and body attitude for an aerodynamic run."""

    velocity_type: VelocityType = Field(
        default=VelocityType.MACH,
        description=(
            "'mach' interprets velocity_value as a Mach number; 'tas' "
            "interprets it as true airspeed in m/s."
        ),
    )
    velocity_value: float = Field(
        gt=0.0,
        description="Mach number (0.05-3.5) or true airspeed in m/s.",
    )
    aoa_deg: float = Field(
        default=0.0,
        ge=-ANGLE_LIMIT_DEG,
        le=ANGLE_LIMIT_DEG,
        description="Angle of attack alpha in degrees, within [-20, +20].",
    )
    sideslip_deg: float = Field(
        default=0.0,
        ge=-ANGLE_LIMIT_DEG,
        le=ANGLE_LIMIT_DEG,
        description="Sideslip angle beta in degrees, within [-20, +20].",
    )
    altitude_m: float | None = Field(
        default=0.0,
        ge=ALTITUDE_MIN_M,
        le=ALTITUDE_MAX_M,
        description=(
            "Geometric altitude for the ISA standard atmosphere. Set to null "
            "and supply static_pressure_pa/static_temperature_k to override."
        ),
    )
    static_pressure_pa: float | None = Field(
        default=None,
        gt=0.0,
        description="Explicit freestream static pressure (Pa), overriding ISA.",
    )
    static_temperature_k: float | None = Field(
        default=None,
        gt=0.0,
        description="Explicit freestream static temperature (K), overriding ISA.",
    )

    @model_validator(mode="after")
    def _validate_conditions(self) -> "FlowParams":
        explicit = (self.static_pressure_pa, self.static_temperature_k)
        if any(v is not None for v in explicit) and not all(
            v is not None for v in explicit
        ):
            raise ValueError(
                "static_pressure_pa and static_temperature_k must be supplied "
                "together when overriding the standard atmosphere"
            )
        if self.altitude_m is None and not all(v is not None for v in explicit):
            raise ValueError(
                "supply either 'altitude_m' or both static pressure and "
                "temperature"
            )
        return self

    def atmosphere(self) -> AtmosphereState:
        """Resolve the freestream thermodynamic state for this condition."""
        if self.static_pressure_pa is not None and self.static_temperature_k is not None:
            return state_from_pressure_temperature(
                self.static_pressure_pa, self.static_temperature_k
            )
        assert self.altitude_m is not None  # guaranteed by the validator
        return isa_state(self.altitude_m)

    def mach(self) -> float:
        """Freestream Mach number, whichever way the speed was specified."""
        if self.velocity_type is VelocityType.MACH:
            return self.velocity_value
        return mach_from_speed(self.velocity_value, self.atmosphere())

    def speed_ms(self) -> float:
        """Freestream true airspeed in m/s."""
        if self.velocity_type is VelocityType.TAS:
            return self.velocity_value
        return speed_from_mach(self.velocity_value, self.atmosphere())

    @model_validator(mode="after")
    def _validate_mach_envelope(self) -> "FlowParams":
        if self.velocity_type is VelocityType.MACH and (
            self.velocity_value > MACH_SUPPORTED_MAX
        ):
            raise ValueError(
                f"Mach {self.velocity_value} exceeds the supported envelope of "
                f"{MACH_SUPPORTED_MAX}"
            )
        return self


# --------------------------------------------------------------------------
# Fin hinge definition
# --------------------------------------------------------------------------


class HingeAxis(StrictModel):
    """A fin hinge line about which the servo torque is evaluated."""

    name: str = Field(
        default="fin_1",
        min_length=1,
        description="Label used in results and plots, e.g. 'fin_pitch_upper'.",
    )
    point: Vector3 = Field(
        description=(
            "A point [x, y, z] on the hinge axis, in metres, in the frame "
            "named by 'frame'."
        ),
    )
    direction: Vector3 = Field(
        description=(
            "Hinge rotation axis direction [u, v, w]. Normalised automatically. "
            "The reported torque is the aerodynamic moment about 'point' "
            "projected onto this axis."
        ),
    )
    frame: Frame = Field(
        default=Frame.SOLVER,
        description=(
            "'rocket': nose along +Z, origin at the reference origin (the "
            "nose tip by default), so a fin near the tail of a 1.3 m rocket "
            "sits near z = -1.2. This is the frame the program shows. "
            "'solver': the wind-tunnel frame, nose at the origin and the body "
            "along +X. Stored projects from before the rocket frame existed "
            "are in 'solver', which is why that is the default here."
        ),
    )
    marker: str | None = Field(
        default=None,
        description=(
            "Optional mesh marker name restricting the torque integration to a "
            "single fin surface, e.g. 'WALL_FINS'. Null integrates all walls."
        ),
    )

    @field_validator("direction")
    @classmethod
    def _normalize_direction(cls, value: list[float]) -> list[float]:
        return list(normalize_vector(value))

    def unit_direction(self) -> tuple[float, float, float]:
        """The normalised hinge axis direction, in the hinge's own frame."""
        return normalize_vector(self.direction)

    def solver_point(self) -> list[float]:
        """The hinge point in the solver frame, whatever frame it was given in."""
        if self.frame is Frame.ROCKET:
            return to_solver(self.point)
        return [float(v) for v in self.point]

    def solver_direction(self) -> list[float]:
        """The unit hinge direction in the solver frame."""
        if self.frame is Frame.ROCKET:
            return to_solver(self.unit_direction())
        return list(self.unit_direction())

    def in_frame(self, frame: Frame) -> "HingeAxis":
        """The same hinge expressed in another frame."""
        if frame is self.frame:
            return self
        if frame is Frame.SOLVER:
            point, direction = self.solver_point(), self.solver_direction()
        else:
            point = to_rocket(self.point)
            direction = to_rocket(self.unit_direction())
        return HingeAxis(
            name=self.name,
            point=point,
            direction=direction,
            marker=self.marker,
            frame=frame,
        )


# --------------------------------------------------------------------------
# Thermal / conjugate heat transfer
# --------------------------------------------------------------------------


class ThermalParams(StrictModel):
    """BMP580 sensor microclimate conjugate-heat-transfer configuration."""

    enclosure_step_path: str = Field(
        description="Absolute path to the 3D-printed enclosure .step/.stp file."
    )
    vehicle_speed_ms: float = Field(
        default=2.0,
        ge=0.0,
        le=50.0,
        description="Vehicle (tram) forward speed driving ram air, in m/s.",
    )
    ambient_temp_c: float = Field(
        default=25.0,
        ge=-50.0,
        le=70.0,
        description="True ambient air temperature in degrees Celsius.",
    )
    solar_flux_w_m2: float = Field(
        default=800.0,
        ge=0.0,
        le=1400.0,
        description="Global solar irradiance on the roof surface, W/m^2.",
    )
    height_above_roof_m: float = Field(
        default=0.1,
        gt=0.0,
        le=2.0,
        description="Height of the enclosure above the irradiated roof, metres.",
    )
    sensor_xyz: Vector3 = Field(
        description=(
            "Coordinates [x, y, z] of the BMP580 die inside the enclosure, in "
            "metres, in the enclosure CAD frame. The air temperature is probed "
            "at exactly this point."
        ),
    )
    housing_conductivity_w_mk: float = Field(
        default=0.18,
        gt=0.0,
        le=500.0,
        description="Thermal conductivity of the printed housing (PLA/PETG ~0.18).",
    )
    housing_density_kg_m3: float = Field(
        default=1240.0,
        gt=0.0,
        description="Density of the housing polymer, kg/m^3.",
    )
    housing_specific_heat_j_kgk: float = Field(
        default=1800.0,
        gt=0.0,
        description="Specific heat capacity of the housing polymer, J/(kg K).",
    )
    housing_emissivity: float = Field(
        default=0.90,
        ge=0.0,
        le=1.0,
        description="Long-wave emissivity of the housing outer surface.",
    )
    housing_solar_absorptivity: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        description="Solar absorptivity of the housing outer surface.",
    )
    roof_emissivity: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="Long-wave emissivity of the vehicle roof surface.",
    )
    roof_solar_absorptivity: float = Field(
        default=0.65,
        ge=0.0,
        le=1.0,
        description="Solar absorptivity of the vehicle roof surface.",
    )
    sky_temperature_k: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Effective radiative sky temperature (K). When null it is estimated "
            "from ambient temperature using the Swinbank clear-sky correlation."
        ),
    )
    sensor_power_w: float = Field(
        default=1.0e-3,
        ge=0.0,
        le=10.0,
        description="Self-heating dissipation of the BMP580 board, watts.",
    )

    def ambient_temp_k(self) -> float:
        """Ambient air temperature in kelvin."""
        return celsius_to_kelvin(self.ambient_temp_c)

    def effective_sky_temperature_k(self) -> float:
        """Radiative sky temperature, using Swinbank when not given explicitly."""
        if self.sky_temperature_k is not None:
            return self.sky_temperature_k
        # Swinbank (1963) clear-sky correlation.
        return 0.0552 * self.ambient_temp_k() ** 1.5


# --------------------------------------------------------------------------
# Solver control
# --------------------------------------------------------------------------


class ConvectiveScheme(str, Enum):
    """Convective flux discretisation schemes exposed by SU2."""

    JST = "JST"
    ROE = "ROE"
    AUSM = "AUSM"
    HLLC = "HLLC"


class TurbulenceModel(str, Enum):
    """RANS turbulence closures."""

    SST = "SST"
    SA = "SA"


class SolverParams(StrictModel):
    """Numerics, parallelism and convergence control for the SU2 run."""

    convective_scheme: ConvectiveScheme | None = Field(
        default=None,
        description=(
            "Convective scheme override. When null it is chosen automatically: "
            "JST below Mach 0.8, Roe upwind at and above it."
        ),
    )
    turbulence_model: TurbulenceModel = Field(
        default=TurbulenceModel.SST,
        description="RANS closure. SST k-omega is the default for both tracks.",
    )
    muscl: bool = Field(
        default=True,
        description="Enable 2nd-order MUSCL reconstruction for upwind schemes.",
    )
    limiter: Literal["VENKATAKRISHNAN", "BARTH_JESPERSEN", "NONE"] = Field(
        default="VENKATAKRISHNAN",
        description="Slope limiter used with MUSCL reconstruction.",
    )
    cfl_number: float | None = Field(
        default=None,
        gt=0.0,
        le=100.0,
        description=(
            "Initial CFL number for the implicit Euler time integration. When "
            "null it is chosen from the flow regime: 5.0 subsonic, 2.0 "
            "transonic, 1.0 at Mach 1.2 and above, where a cold start is "
            "fragile. An explicit value is always used as given."
        ),
    )
    cfl_adapt: bool = Field(
        default=True,
        description="Enable SU2's adaptive CFL ramping.",
    )
    max_iterations: int = Field(
        default=5000,
        ge=1,
        le=100_000,
        description="Hard iteration cap for the solver.",
    )
    convergence_residual: float = Field(
        default=-5.0,
        le=0.0,
        description=(
            "Convergence threshold as log10 of RMS[Rho]. -5.0 means the run "
            "stops once the density residual has dropped five orders."
        ),
    )
    force_stabilization_window: int = Field(
        default=250,
        ge=10,
        le=5000,
        description=(
            "Iteration window over which the drag coefficient must be stable "
            "for early termination on force convergence."
        ),
    )
    force_stabilization_tol: float = Field(
        default=1.0e-4,
        gt=0.0,
        description=(
            "Relative standard deviation of C_d within the window below which "
            "the forces are considered steady."
        ),
    )
    mpi_ranks: int = Field(
        default=10,
        ge=1,
        le=64,
        description=(
            "MPI rank count passed to msmpiexec. Defaults to 10, leaving two "
            "threads of the 6C/12T target machine for the GUI."
        ),
    )
    restart: bool = Field(
        default=False,
        description="Restart from a previous solution file if one is present.",
    )
    stall_timeout_s: float = Field(
        default=300.0,
        gt=0.0,
        description=(
            "Give up if the solver produces no output at all for this many "
            "seconds. A solver that has stopped talking has stopped working, "
            "usually because an aborted MPI job left a rank holding the "
            "output pipe open."
        ),
    )
    wall_time_limit_s: float = Field(
        default=7200.0,
        gt=0.0,
        description=(
            "Give up after this many seconds in total, however chatty the run "
            "has been. Catches a solve that never converges."
        ),
    )
    rescue_on_divergence: bool = Field(
        default=True,
        description=(
            "When the solve blows up, automatically retry it as a first-order "
            "low-CFL stage followed by a second-order restart from that "
            "solution. A rescued result is flagged as such."
        ),
    )
    rescue_iterations: int = Field(
        default=400,
        ge=1,
        le=100_000,
        description="Iteration budget for the first-order rescue stage.",
    )
    rescue_cfl: float = Field(
        default=0.5,
        gt=0.0,
        le=100.0,
        description="Starting CFL for the first-order rescue stage.",
    )


class ReferenceValues(StrictModel):
    """Reference area, length and moment origin for coefficient normalisation."""

    reference_area_m2: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Reference area for force coefficients. When null the maximum "
            "body cross-section (pi*D_ref^2/4) measured from the CAD is used."
        ),
    )
    reference_length_m: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "Reference length for moment coefficients. When null the body "
            "diameter D_ref measured from the CAD is used."
        ),
    )
    moment_origin: Vector3 | None = Field(
        default=None,
        description=(
            "Moment reference origin [x, y, z] in wind-tunnel coordinates. "
            "When null the aligned reference origin (nose tip) is used."
        ),
    )


# --------------------------------------------------------------------------
# Aggregate run requests
# --------------------------------------------------------------------------


class MeshRequest(StrictModel):
    """Everything needed to produce a solver-ready mesh."""

    geometry: GeometryParams
    domain: DomainParams = Field(default_factory=DomainParams)
    mesh: MeshParams = Field(default_factory=MeshParams)
    sizing_flow: FlowParams | None = Field(
        default=None,
        description=(
            "Optional flow condition used only to size the boundary layer for "
            "the requested y+. When null a Mach 1.0 sea-level condition is "
            "assumed, which is conservative across the supported envelope."
        ),
    )


class AeroRunRequest(StrictModel):
    """A single aerodynamic simulation point."""

    mesh_id: str = Field(description="Identifier of a mesh produced earlier.")
    flow: FlowParams
    solver: SolverParams = Field(default_factory=SolverParams)
    reference: ReferenceValues = Field(default_factory=ReferenceValues)
    hinge_axes: list[HingeAxis] = Field(
        default_factory=list,
        description="Fin hinge axes to evaluate servo torque about.",
    )


class ThermalRunRequest(StrictModel):
    """A single conjugate-heat-transfer simulation point."""

    mesh_id: str = Field(description="Identifier of a thermal mesh produced earlier.")
    thermal: ThermalParams
    solver: SolverParams = Field(default_factory=SolverParams)


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


class HingeTorqueResult(StrictModel):
    """Servo torque about one fin hinge axis."""

    name: str = Field(description="Hinge axis label.")
    torque_nm: float = Field(
        description="Scalar torque about the hinge axis, N*m. Positive follows "
        "the right-hand rule about the axis direction."
    )
    moment_vector_nm: Vector3 = Field(
        description=(
            "Full moment vector about the hinge point, N*m, in the frame the "
            "hinge was given in."
        )
    )
    frame: Frame = Field(
        default=Frame.SOLVER,
        description="Frame of 'moment_vector_nm': the hinge's own.",
    )


class AeroResult(StrictModel):
    """Converged aerodynamic output for one simulation point."""

    sim_id: str
    mesh_id: str
    mach: float
    speed_ms: float
    aoa_deg: float
    sideslip_deg: float
    dynamic_pressure_pa: float
    reference_area_m2: float
    reference_length_m: float
    force_x_n: float = Field(description="Body-axis force component F_x, N.")
    force_y_n: float = Field(description="Body-axis force component F_y, N.")
    force_z_n: float = Field(description="Body-axis force component F_z, N.")
    drag_n: float = Field(description="Wind-axis drag force, N.")
    lift_n: float = Field(description="Wind-axis lift force, N.")
    sideforce_n: float = Field(description="Wind-axis side force, N.")
    cd: float = Field(description="Drag coefficient.")
    cl: float = Field(description="Lift coefficient.")
    cs: float = Field(description="Side-force coefficient.")
    cm_pitch: float = Field(description="Pitching-moment coefficient.")
    center_of_pressure: Vector3 = Field(
        description="Centre of pressure [x, y, z] in wind-tunnel coordinates, m."
    )
    force_rocket_n: Vector3 = Field(
        description=(
            "Total aerodynamic force [F_x, F_y, F_z] in the rocket frame, N: "
            "nose along +Z, so drag appears as a negative F_z on a rocket "
            "climbing nose-first, and the lift from a positive angle of "
            "attack along +X. This is the answer to 'what force acts along "
            "each axis of the rocket'."
        )
    )
    center_of_pressure_rocket: Vector3 = Field(
        description=(
            "Centre of pressure in the rocket frame, m. Its Z is negative: "
            "the distance behind the reference origin (the nose tip by "
            "default). Undefined (NaN) at zero incidence on a symmetric body."
        )
    )
    hinge_torques: list[HingeTorqueResult] = Field(default_factory=list)
    iterations: int = Field(description="Iterations completed.")
    final_residual_rho: float = Field(description="Final log10 RMS[Rho].")
    converged: bool
    wall_time_s: float
    rescued: bool = Field(
        default=False,
        description=(
            "True when the first attempt blew up and the result comes from "
            "the automatic first-order rescue. The numbers are still valid, "
            "but the case needed help to get there and that is worth knowing."
        ),
    )
    notes: list[str] = Field(
        default_factory=list,
        description=(
            "Remarks about how this result was obtained, including solver "
            "warnings and any rescue that was performed."
        ),
    )


class ThermalResult(StrictModel):
    """Converged conjugate-heat-transfer output for one sensor case."""

    sim_id: str
    mesh_id: str
    sensor_temp_k: float = Field(
        description="Air temperature at the BMP580 die coordinate, K."
    )
    sensor_temp_c: float = Field(
        description="Air temperature at the BMP580 die coordinate, degrees C."
    )
    ambient_temp_c: float
    delta_t_error_k: float = Field(
        description="Measurement bias T_BMP580 - T_ambient, kelvin."
    )
    roof_temp_k: float = Field(description="Area-averaged roof surface temperature, K.")
    housing_max_temp_k: float = Field(
        description="Peak temperature anywhere in the housing solid, K."
    )
    chamber_mean_temp_k: float = Field(
        description="Volume-averaged internal chamber air temperature, K."
    )
    tube_mass_flow_kg_s: float = Field(
        description="Ram-air mass flow ingested through the sampling tube, kg/s."
    )
    iterations: int
    converged: bool
    wall_time_s: float


class MeshResult(StrictModel):
    """Report produced by the meshing pipeline."""

    mesh_id: str
    mesh_path: str
    cell_count: int
    node_count: int
    surface_element_count: int
    target_band: tuple[int, int]
    within_target_band: bool
    reference_length_m: float
    reference_diameter_m: float
    reference_area_m2: float
    first_cell_height_m: float
    estimated_yplus: float
    boundary_markers: dict[str, int] = Field(
        description="Mapping of physical group name to element count."
    )
    healing_report: dict[str, int] = Field(default_factory=dict)
    min_quality: float = Field(description="Minimum scaled-Jacobian cell quality.")
    targeting_iterations: int
    wall_time_s: float
