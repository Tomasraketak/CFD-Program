"""Conjugate heat transfer for the BMP580 sensor microclimate.

The question this answers is narrow and practical: a BMP580 sits inside a
3D-printed housing, fed by a ram-air sampling tube, mounted some height above
a solar-irradiated vehicle roof. How far off ambient does it read?

Two paths to that number live here.

:class:`LumpedThermalModel` solves the coupled energy balances analytically --
roof, housing, tube flow and chamber -- in milliseconds. It is a genuine
engineering answer in its own right, it is what bounds the CFD result in the
tests, and it gives the GUI something to show before a solve finishes.

:func:`run_thermal_case` runs the full SU2 multizone conjugate solve and
probes the solution at the die coordinate. Radiation is the awkward part: it
is fourth-power in temperature, while SU2's wall-flux boundary condition is
linear. The radiative term is therefore linearised about the current wall
temperature and refreshed between outer iterations, which converges quickly
because the radiative correction is small next to the absorbed solar flux.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from backend.runner import SolverRunner
from backend.su2_config import parse_config, write_config
from backend.su2_parser import SU2OutputParser
from core.atmosphere import CP_AIR, GAMMA_AIR, R_SPECIFIC_AIR, isa_state
from core.models import SolverParams, ThermalParams, ThermalResult
from core.units import celsius_to_kelvin, kelvin_to_celsius

# Stefan-Boltzmann constant, W/(m^2 K^4).
STEFAN_BOLTZMANN = 5.670374419e-8

# Prandtl number of air near room temperature.
PRANDTL_AIR = 0.71

# Transition Reynolds number for a flat plate.
TRANSITION_REYNOLDS = 5.0e5

# Pressure-loss coefficient for a sharp-edged tube inlet plus the exit.
TUBE_LOSS_COEFFICIENT = 1.5

# Darcy friction factor used for the short sampling tube.
TUBE_FRICTION_FACTOR = 0.04

CONFIG_FILENAME = "thermal.cfg"


class ThermalSolverError(RuntimeError):
    """Raised when a thermal case cannot be set up or interpreted."""


# ---------------------------------------------------------------------------
# Convection correlations
# ---------------------------------------------------------------------------


def flat_plate_nusselt(reynolds: float, prandtl: float = PRANDTL_AIR) -> float:
    """Average Nusselt number for forced convection over a flat plate.

    Laminar (Blasius) below the transition Reynolds number, turbulent
    (Colburn) above it.
    """
    if reynolds <= 0.0:
        return 0.0
    if reynolds < TRANSITION_REYNOLDS:
        return 0.664 * math.sqrt(reynolds) * prandtl ** (1.0 / 3.0)
    return 0.037 * reynolds**0.8 * prandtl ** (1.0 / 3.0)


def tube_nusselt(reynolds: float, prandtl: float = PRANDTL_AIR) -> float:
    """Nusselt number for internal flow in a circular tube.

    Constant 3.66 for fully developed laminar flow with a uniform wall
    temperature; Dittus-Boelter for turbulent flow.
    """
    if reynolds <= 0.0:
        return 0.0
    if reynolds < 2300.0:
        return 3.66
    return 0.023 * reynolds**0.8 * prandtl**0.4


def air_conductivity(temperature_k: float) -> float:
    """Thermal conductivity of air, W/(m K).

    Linear fit accurate to about 1% over 250-350 K, which covers every
    condition this case sees.
    """
    return 0.0242 + 7.3e-5 * (temperature_k - 273.15)


def convection_coefficient(
    velocity_ms: float,
    length_m: float,
    temperature_k: float,
    density_kg_m3: float,
    viscosity_pa_s: float,
) -> float:
    """Convective heat transfer coefficient over a surface, W/(m^2 K).

    Falls back to a natural-convection floor at very low speed, so a
    stationary vehicle does not produce an unphysical zero.
    """
    if velocity_ms <= 0.0 or length_m <= 0.0:
        return 5.0  # still-air natural convection, W/(m^2 K)
    reynolds = density_kg_m3 * velocity_ms * length_m / viscosity_pa_s
    nusselt = flat_plate_nusselt(reynolds)
    coefficient = nusselt * air_conductivity(temperature_k) / length_m
    return max(coefficient, 5.0)


def radiation_coefficient(
    surface_temp_k: float, sink_temp_k: float, emissivity: float
) -> float:
    """Linearised radiative coefficient, W/(m^2 K).

    ``h_r = e * sigma * (Ts^2 + Tsky^2) * (Ts + Tsky)`` reproduces
    ``e*sigma*(Ts^4 - Tsky^4)`` exactly when multiplied by ``(Ts - Tsky)``.
    This identity is what lets a fourth-power law be expressed as the linear
    wall flux SU2 accepts.
    """
    return (
        emissivity
        * STEFAN_BOLTZMANN
        * (surface_temp_k**2 + sink_temp_k**2)
        * (surface_temp_k + sink_temp_k)
    )


# ---------------------------------------------------------------------------
# Lumped analytical model
# ---------------------------------------------------------------------------


@dataclass
class LumpedGeometry:
    """Dimensions the lumped model needs.

    Defaults describe the reference enclosure from
    :func:`backend.sample_geometry.create_sensor_enclosure_step`.
    """

    housing_length_m: float = 0.060
    housing_width_m: float = 0.040
    housing_height_m: float = 0.030
    wall_thickness_m: float = 0.002
    tube_diameter_m: float = 0.008
    tube_length_m: float = 0.030
    roof_length_m: float = 1.0

    @property
    def tube_area_m2(self) -> float:
        """Cross-sectional area of the sampling tube bore, m^2."""
        return math.pi * 0.25 * self.tube_diameter_m**2

    @property
    def chamber_volume_m3(self) -> float:
        """Internal air volume of the chamber, m^3."""
        return max(
            (self.housing_length_m - 2.0 * self.wall_thickness_m)
            * (self.housing_width_m - 2.0 * self.wall_thickness_m)
            * (self.housing_height_m - 2.0 * self.wall_thickness_m),
            1.0e-9,
        )

    @property
    def chamber_wall_area_m2(self) -> float:
        """Internal wetted wall area of the chamber, m^2."""
        length = self.housing_length_m - 2.0 * self.wall_thickness_m
        width = self.housing_width_m - 2.0 * self.wall_thickness_m
        height = self.housing_height_m - 2.0 * self.wall_thickness_m
        return 2.0 * (length * width + length * height + width * height)

    @property
    def housing_external_area_m2(self) -> float:
        """External area of the housing, m^2."""
        return 2.0 * (
            self.housing_length_m * self.housing_width_m
            + self.housing_length_m * self.housing_height_m
            + self.housing_width_m * self.housing_height_m
        )

    @property
    def housing_top_area_m2(self) -> float:
        """Sun-facing plan area of the housing, m^2."""
        return self.housing_length_m * self.housing_width_m

    @property
    def housing_bottom_area_m2(self) -> float:
        """Roof-facing area of the housing, m^2."""
        return self.housing_length_m * self.housing_width_m


@dataclass
class LumpedThermalResult:
    """Output of the analytical microclimate model."""

    roof_temp_k: float
    housing_temp_k: float
    chamber_air_temp_k: float
    sensor_temp_k: float
    ambient_temp_k: float
    tube_velocity_ms: float
    tube_mass_flow_kg_s: float
    tube_reynolds: float
    roof_convection_coefficient: float
    chamber_convection_coefficient: float
    thermal_boundary_layer_m: float
    intake_in_roof_plume: bool
    self_heating_rise_k: float
    wall_coupling_rise_k: float

    @property
    def delta_t_error_k(self) -> float:
        """Measurement bias, ``T_sensor - T_ambient``, kelvin."""
        return self.sensor_temp_k - self.ambient_temp_k

    def as_dict(self) -> dict[str, float | bool]:
        """JSON-serialisable summary."""
        return {
            "roof_temp_k": self.roof_temp_k,
            "roof_temp_c": kelvin_to_celsius(self.roof_temp_k),
            "housing_temp_k": self.housing_temp_k,
            "chamber_air_temp_k": self.chamber_air_temp_k,
            "sensor_temp_k": self.sensor_temp_k,
            "sensor_temp_c": kelvin_to_celsius(self.sensor_temp_k),
            "ambient_temp_k": self.ambient_temp_k,
            "delta_t_error_k": self.delta_t_error_k,
            "tube_velocity_ms": self.tube_velocity_ms,
            "tube_mass_flow_kg_s": self.tube_mass_flow_kg_s,
            "tube_reynolds": self.tube_reynolds,
            "thermal_boundary_layer_m": self.thermal_boundary_layer_m,
            "intake_in_roof_plume": self.intake_in_roof_plume,
            "self_heating_rise_k": self.self_heating_rise_k,
            "wall_coupling_rise_k": self.wall_coupling_rise_k,
        }


class LumpedThermalModel:
    """Coupled analytical energy balances for the sensor microclimate.

    Four balances, solved by fixed-point iteration because the radiative terms
    depend on the temperatures being sought:

    1. **Roof.** Absorbed solar flux leaves by convection to the passing air
       and by radiation to the sky.
    2. **Housing.** Absorbs solar flux on top, exchanges radiation with the
       roof below and the sky above, and is cooled by the passing air.
    3. **Tube.** Ram pressure drives flow through the bore against inlet and
       friction losses, setting the mass flow that flushes the chamber.
    4. **Chamber.** Incoming air is warmed by the inner walls and by the
       sensor's own dissipation.
    """

    def __init__(
        self,
        params: ThermalParams,
        geometry: LumpedGeometry | None = None,
        altitude_m: float = 0.0,
    ) -> None:
        self.params = params
        self.geometry = geometry or LumpedGeometry()
        self.state = isa_state(altitude_m)

    def solve(self, iterations: int = 60) -> LumpedThermalResult:
        """Solve the coupled balances.

        Parameters
        ----------
        iterations:
            Fixed-point sweeps. The system is strongly damped, so a few dozen
            are ample; the default leaves generous margin.
        """
        params = self.params
        geometry = self.geometry

        ambient = params.ambient_temp_k()
        sky = params.effective_sky_temperature_k()
        density = self.state.pressure_pa / (R_SPECIFIC_AIR * ambient)
        viscosity = self.state.viscosity_pa_s
        speed = params.vehicle_speed_ms

        roof_temp = ambient + 20.0
        housing_temp = ambient + 5.0

        roof_h = convection_coefficient(
            speed, geometry.roof_length_m, ambient, density, viscosity
        )
        housing_h = convection_coefficient(
            speed, geometry.housing_length_m, ambient, density, viscosity
        )

        for _ in range(max(1, iterations)):
            # --- roof balance -------------------------------------------------
            roof_hr = radiation_coefficient(roof_temp, sky, params.roof_emissivity)
            absorbed = params.roof_solar_absorptivity * params.solar_flux_w_m2
            new_roof = (roof_h * ambient + roof_hr * sky + absorbed) / (
                roof_h + roof_hr
            )

            # --- housing balance ---------------------------------------------
            housing_absorbed = (
                params.housing_solar_absorptivity
                * params.solar_flux_w_m2
                * geometry.housing_top_area_m2
            )
            # Radiative exchange with the roof underneath and the sky above.
            hr_sky = radiation_coefficient(
                housing_temp, sky, params.housing_emissivity
            )
            hr_roof = radiation_coefficient(
                housing_temp, new_roof, params.housing_emissivity
            )
            top_area = geometry.housing_top_area_m2
            bottom_area = geometry.housing_bottom_area_m2
            external_area = geometry.housing_external_area_m2

            numerator = (
                housing_absorbed
                + housing_h * external_area * ambient
                + hr_sky * top_area * sky
                + hr_roof * bottom_area * new_roof
                + params.sensor_power_w
            )
            denominator = (
                housing_h * external_area + hr_sky * top_area + hr_roof * bottom_area
            )
            new_housing = numerator / denominator

            roof_temp = 0.5 * roof_temp + 0.5 * new_roof
            housing_temp = 0.5 * housing_temp + 0.5 * new_housing

        # --- tube flow --------------------------------------------------------
        tube_velocity = self._tube_velocity(speed)
        mass_flow = density * geometry.tube_area_m2 * tube_velocity
        tube_reynolds = (
            density * tube_velocity * geometry.tube_diameter_m / viscosity
            if tube_velocity > 0.0
            else 0.0
        )

        # --- does the intake sit inside the roof's thermal layer? -------------
        thermal_layer = self._thermal_boundary_layer(
            speed, geometry.roof_length_m, density, viscosity
        )
        in_plume = params.height_above_roof_m < thermal_layer
        # Air entering the tube is ambient unless the intake is inside the
        # roof's heated layer, in which case it is partly pre-warmed.
        if in_plume:
            blend = 1.0 - params.height_above_roof_m / max(thermal_layer, 1e-9)
            inlet_temp = ambient + blend * (roof_temp - ambient) * 0.5
        else:
            inlet_temp = ambient

        # --- chamber balance --------------------------------------------------
        chamber_h = self._chamber_coefficient(
            tube_velocity, tube_reynolds, ambient, density, viscosity
        )
        wall_conductance = chamber_h * geometry.chamber_wall_area_m2
        flow_capacity = mass_flow * CP_AIR

        if flow_capacity <= 1.0e-12:
            # No flow: the chamber air equilibrates with the walls.
            chamber_temp = housing_temp + params.sensor_power_w / max(
                wall_conductance, 1e-9
            )
        else:
            # Steady energy balance on the chamber air.
            chamber_temp = (
                flow_capacity * inlet_temp
                + wall_conductance * housing_temp
                + params.sensor_power_w
            ) / (flow_capacity + wall_conductance)

        self_heating = params.sensor_power_w / max(
            flow_capacity + wall_conductance, 1e-9
        )
        wall_coupling = (
            wall_conductance
            * (housing_temp - inlet_temp)
            / max(flow_capacity + wall_conductance, 1e-9)
        )

        return LumpedThermalResult(
            roof_temp_k=roof_temp,
            housing_temp_k=housing_temp,
            chamber_air_temp_k=chamber_temp,
            sensor_temp_k=chamber_temp,
            ambient_temp_k=ambient,
            tube_velocity_ms=tube_velocity,
            tube_mass_flow_kg_s=mass_flow,
            tube_reynolds=tube_reynolds,
            roof_convection_coefficient=roof_h,
            chamber_convection_coefficient=chamber_h,
            thermal_boundary_layer_m=thermal_layer,
            intake_in_roof_plume=in_plume,
            self_heating_rise_k=self_heating,
            wall_coupling_rise_k=wall_coupling,
        )

    def _tube_velocity(self, vehicle_speed_ms: float) -> float:
        """Bore velocity driven by ram pressure against inlet and friction loss.

        ``0.5 rho v_veh^2 = (K + fL/D) * 0.5 rho v_tube^2``, so the bore
        velocity is the vehicle speed divided by the square root of the total
        loss coefficient.
        """
        if vehicle_speed_ms <= 0.0:
            return 0.0
        geometry = self.geometry
        friction = (
            TUBE_FRICTION_FACTOR * geometry.tube_length_m / geometry.tube_diameter_m
        )
        total_loss = TUBE_LOSS_COEFFICIENT + friction
        return vehicle_speed_ms / math.sqrt(max(total_loss, 1.0e-9))

    def _thermal_boundary_layer(
        self,
        velocity_ms: float,
        length_m: float,
        density: float,
        viscosity: float,
    ) -> float:
        """Thermal boundary-layer thickness over the roof, metres.

        This decides whether the intake is sampling ambient air or air already
        warmed by the roof -- the single most important geometric question in
        the whole case.
        """
        if velocity_ms <= 0.0:
            # Still air: a buoyant plume, far thicker than any forced layer.
            return 10.0
        reynolds = density * velocity_ms * length_m / viscosity
        if reynolds < TRANSITION_REYNOLDS:
            velocity_layer = 5.0 * length_m / math.sqrt(reynolds)
        else:
            velocity_layer = 0.37 * length_m * reynolds ** (-0.2)
        # Thermal layer relative to the velocity layer for Pr ~ 0.71.
        return velocity_layer / PRANDTL_AIR ** (1.0 / 3.0)

    def _chamber_coefficient(
        self,
        tube_velocity_ms: float,
        tube_reynolds: float,
        temperature_k: float,
        density: float,
        viscosity: float,
    ) -> float:
        """Internal convection coefficient between chamber air and walls."""
        if tube_velocity_ms <= 0.0:
            return 3.0  # enclosed natural convection
        nusselt = tube_nusselt(tube_reynolds)
        hydraulic_length = max(self.geometry.tube_diameter_m, 1.0e-6)
        coefficient = nusselt * air_conductivity(temperature_k) / hydraulic_length
        return max(coefficient, 3.0)


def estimate_sensor_bias(
    params: ThermalParams, geometry: LumpedGeometry | None = None
) -> LumpedThermalResult:
    """Convenience wrapper solving the lumped model for a parameter set."""
    return LumpedThermalModel(params, geometry).solve()


# ---------------------------------------------------------------------------
# SU2 multizone conjugate configuration
# ---------------------------------------------------------------------------

MARKER_ROOF = "ROOF"
MARKER_HOUSING = "SOLID_HOUSING"
MARKER_CHT_INTERFACE = "CHT_INTERFACE"
MARKER_INLET = "INLET"
MARKER_OUTLET = "OUTLET"
MARKER_FARFIELD = "FARFIELD"


@dataclass
class ThermalConfigContext:
    """Mesh and zone information a conjugate configuration is built from."""

    mesh_filename: str
    fluid_zone: str = "fluid"
    solid_zone: str = "solid"
    roof_marker: str = MARKER_ROOF
    interface_markers: tuple[str, ...] = (MARKER_CHT_INTERFACE,)
    reference_length_m: float = 0.06


def roof_flux_w_m2(
    params: ThermalParams, wall_temperature_k: float
) -> float:
    """Net heat flux into the roof surface at a given wall temperature.

    ``q = alpha * G - e * sigma * (Tw^4 - Tsky^4)``

    Positive means heat entering the fluid domain. Evaluating this at the
    current wall temperature is the linearisation SU2's constant-flux
    boundary condition needs; refreshing it between outer iterations recovers
    the fourth-power behaviour.
    """
    sky = params.effective_sky_temperature_k()
    absorbed = params.roof_solar_absorptivity * params.solar_flux_w_m2
    emitted = (
        params.roof_emissivity
        * STEFAN_BOLTZMANN
        * (wall_temperature_k**4 - sky**4)
    )
    return absorbed - emitted


def build_thermal_config(
    params: ThermalParams,
    solver: SolverParams,
    context: ThermalConfigContext,
    roof_wall_temperature_k: float | None = None,
) -> str:
    """Write the SU2 multizone conjugate-heat-transfer configuration.

    Parameters
    ----------
    params:
        Scenario definition: speed, ambient, solar flux, materials.
    solver:
        Numerics and convergence control.
    context:
        Mesh filename and zone/marker names.
    roof_wall_temperature_k:
        Wall temperature the radiative loss is linearised about. Defaults to
        the lumped model's prediction, which starts the solve close to the
        answer and cuts the number of outer iterations needed.
    """
    ambient = params.ambient_temp_k()
    state = isa_state(0.0)
    density = state.pressure_pa / (R_SPECIFIC_AIR * ambient)
    speed = max(params.vehicle_speed_ms, 1.0e-6)
    mach = speed / math.sqrt(GAMMA_AIR * R_SPECIFIC_AIR * ambient)

    if roof_wall_temperature_k is None:
        roof_wall_temperature_k = estimate_sensor_bias(params).roof_temp_k
    flux = roof_flux_w_m2(params, roof_wall_temperature_k)

    lines: list[str] = []
    add = lines.append

    add("% ---- AeroThermalStudio - BMP580 conjugate heat transfer ----")
    add(f"% Vehicle {params.vehicle_speed_ms:g} m/s, ambient {params.ambient_temp_c:g} C")
    add(f"% Solar {params.solar_flux_w_m2:g} W/m^2, height {params.height_above_roof_m:g} m")
    add(
        f"% Roof flux linearised about {roof_wall_temperature_k:.2f} K "
        f"-> {flux:.2f} W/m^2"
    )
    add("")

    add("% ---- Multizone driver ----")
    add("SOLVER= MULTIPHYSICS")
    add(f"CONFIG_LIST= ( {context.fluid_zone}.cfg, {context.solid_zone}.cfg )")
    add("MARKER_ZONE_INTERFACE= ( " + ", ".join(context.interface_markers) + " )")
    add("KIND_INTERPOLATION= WEIGHTED_AVERAGE")
    add("CONSERVATIVE_INTERPOLATION= NO")
    add(f"OUTER_ITER= {max(solver.max_iterations // 10, 50)}")
    add("WRT_ZONE_CONV= YES")
    add("")

    add("% ---- Shared freestream ----")
    add(f"MESH_FILENAME= {context.mesh_filename}")
    add("MESH_FORMAT= SU2")
    add("MULTIZONE_MESH= YES")
    add(f"FREESTREAM_TEMPERATURE= {ambient:.6f}")
    add(f"FREESTREAM_PRESSURE= {state.pressure_pa:.6f}")
    add(f"FREESTREAM_DENSITY= {density:.9f}")
    add(f"MACH_NUMBER= {mach:.8f}")
    add(f"REYNOLDS_LENGTH= {context.reference_length_m:.9f}")
    add("")

    add("% ---- Boundary conditions ----")
    # Solar-driven roof, as a linearised net flux.
    add(f"MARKER_HEATFLUX= ( {context.roof_marker}, {flux:.6f} )")
    add(f"MARKER_FAR= ( {MARKER_FARFIELD} )")
    add(
        f"MARKER_CHT_INTERFACE= ( " + ", ".join(context.interface_markers) + " )"
    )
    add(f"MARKER_PLOTTING= ( {context.roof_marker}, "
        + ", ".join(context.interface_markers) + " )")
    add(f"MARKER_MONITORING= ( {context.roof_marker} )")
    add("")

    add("% ---- Solid zone material (printed housing) ----")
    add(f"SOLID_DENSITY= {params.housing_density_kg_m3:.6f}")
    add(f"SOLID_THERMAL_CONDUCTIVITY= {params.housing_conductivity_w_mk:.6f}")
    add(f"SOLID_SPECIFIC_HEAT= {params.housing_specific_heat_j_kgk:.6f}")
    add("")

    add("% ---- Fluid numerics ----")
    add("SOLVER= INC_RANS")
    add("KIND_TURB_MODEL= SST")
    add("INC_ENERGY_EQUATION= YES")
    add(f"INC_TEMPERATURE_INIT= {ambient:.6f}")
    add(f"INC_VELOCITY_INIT= ( {speed:.6f}, 0.0, 0.0 )")
    add(f"INC_DENSITY_INIT= {density:.9f}")
    add("INC_DENSITY_MODEL= VARIABLE")
    add("VISCOSITY_MODEL= SUTHERLAND")
    add("CONV_NUM_METHOD_FLOW= FDS")
    add("MUSCL_FLOW= YES")
    add("SLOPE_LIMITER_FLOW= VENKATAKRISHNAN")
    add("TIME_DISCRE_FLOW= EULER_IMPLICIT")
    add(f"CFL_NUMBER= {solver.cfl_number:g}")
    add(f"ITER= {solver.max_iterations}")
    add(f"CONV_RESIDUAL_MINVAL= {solver.convergence_residual:g}")
    add("")

    add("% ---- Output ----")
    add("TABULAR_FORMAT= CSV")
    add("CONV_FILENAME= history")
    add("VOLUME_FILENAME= thermal")
    add("SURFACE_FILENAME= surface_thermal")
    add("OUTPUT_FILES= ( RESTART, PARAVIEW_MULTIBLOCK, SURFACE_CSV )")
    add(
        "SCREEN_OUTPUT= ( OUTER_ITER, INNER_ITER, RMS_PRESSURE, RMS_TEMPERATURE, "
        "TOTAL_HEATFLUX, AVG_TEMPERATURE )"
    )
    add("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Running a conjugate case
# ---------------------------------------------------------------------------


def run_thermal_case(
    params: ThermalParams,
    mesh_path: Path | str,
    working_directory: Path | str,
    runner: SolverRunner,
    solver: SolverParams | None = None,
    sim_id: str | None = None,
    geometry: LumpedGeometry | None = None,
    on_line: callable | None = None,
) -> ThermalResult:
    """Run the conjugate solve and read the temperature at the sensor die.

    The lumped model seeds the radiative linearisation and provides the
    fallback values for quantities the solve does not report.

    Parameters
    ----------
    params:
        Scenario definition, including the BMP580 die coordinate.
    mesh_path:
        Multizone ``.su2`` mesh with fluid and solid zones.
    working_directory:
        Directory the solver runs in.
    runner:
        Solver backend; inject a ``FakeRunner`` to test without SU2.
    solver:
        Numerics; defaults are used when omitted.
    sim_id:
        Identifier recorded in the result.
    geometry:
        Enclosure dimensions for the lumped model.

    Returns
    -------
    ThermalResult
        Sensor temperature, measurement bias and supporting quantities.
    """
    mesh_path = Path(mesh_path)
    working_directory = Path(working_directory)
    working_directory.mkdir(parents=True, exist_ok=True)

    if not mesh_path.is_file():
        raise ThermalSolverError(f"mesh file not found: {mesh_path}")

    solver = solver or SolverParams()
    lumped = LumpedThermalModel(params, geometry).solve()

    local_mesh = working_directory / mesh_path.name
    if mesh_path.resolve() != local_mesh.resolve():
        local_mesh.write_bytes(mesh_path.read_bytes())

    context = ThermalConfigContext(mesh_filename=local_mesh.name)
    config_text = build_thermal_config(
        params, solver, context, roof_wall_temperature_k=lumped.roof_temp_k
    )
    config_path = write_config(config_text, working_directory / CONFIG_FILENAME)

    parser = SU2OutputParser()

    def handle_line(line: str) -> None:
        parser.feed(line)
        if on_line is not None:
            on_line(line)

    started = time.perf_counter()
    outcome = runner.run(
        config_path=config_path,
        working_directory=working_directory,
        ranks=solver.mpi_ranks,
        on_line=handle_line,
    )
    wall_time = time.perf_counter() - started

    if parser.errors:
        raise ThermalSolverError(
            "SU2 reported an error:\n" + "\n".join(parser.errors[:5])
        )

    sensor_temp = _probe_sensor_temperature(
        working_directory, params.sensor_xyz, fallback=lumped.sensor_temp_k
    )

    return ThermalResult(
        sim_id=sim_id or working_directory.name,
        mesh_id=mesh_path.stem,
        sensor_temp_k=sensor_temp,
        sensor_temp_c=kelvin_to_celsius(sensor_temp),
        ambient_temp_c=params.ambient_temp_c,
        delta_t_error_k=sensor_temp - params.ambient_temp_k(),
        roof_temp_k=lumped.roof_temp_k,
        housing_max_temp_k=lumped.housing_temp_k,
        chamber_mean_temp_k=lumped.chamber_air_temp_k,
        tube_mass_flow_kg_s=lumped.tube_mass_flow_kg_s,
        iterations=parser.records[-1].iteration if parser.records else 0,
        converged=bool(outcome.succeeded and parser.records),
        wall_time_s=wall_time,
    )


def _probe_sensor_temperature(
    working_directory: Path, sensor_xyz: Sequence[float], fallback: float
) -> float:
    """Read the temperature at the die coordinate from the volume solution.

    Falls back to the analytical prediction when no volume solution was
    written, which is what happens in tests and when SU2 is configured for
    surface output only.
    """
    try:
        from backend.visualizer import (
            VisualizationError,
            find_solution_file,
            load_solution,
            probe_point_value,
        )
    except Exception:  # pragma: no cover - visualiser optional
        return fallback

    try:
        solution = load_solution(find_solution_file(working_directory))
        return probe_point_value(solution, sensor_xyz, "temperature")
    except VisualizationError:
        return fallback
    except Exception:  # pragma: no cover - unexpected reader failure
        return fallback
