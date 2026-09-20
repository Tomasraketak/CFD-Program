"""Aerodynamic similarity parameters and boundary-layer sizing helpers.

Everything the mesh pipeline and solver configuration need in order to turn
operator-facing quantities (Mach number, true airspeed, altitude) into the
non-dimensional inputs SU2 expects, plus the wall-normal spacing required to
land the first cell centroid in a chosen y+ band.
"""

from __future__ import annotations

import math

from core.atmosphere import GAMMA_AIR, R_SPECIFIC_AIR, AtmosphereState

# Wall-function friendly y+ band. Below ~30 the log-law is invalid; above ~300
# the first cell no longer resolves the log layer.
YPLUS_WALL_FUNCTION_MIN = 30.0
YPLUS_WALL_FUNCTION_MAX = 300.0

# Regime thresholds used to pick the convective scheme.
MACH_SUBSONIC_MAX = 0.8
MACH_SUPPORTED_MAX = 3.5


def mach_from_speed(speed_ms: float, state: AtmosphereState) -> float:
    """Convert true airspeed (m/s) to Mach number at the given state."""
    if speed_ms < 0.0:
        raise ValueError(f"speed must be non-negative, got {speed_ms} m/s")
    return speed_ms / state.speed_of_sound_ms


def speed_from_mach(mach: float, state: AtmosphereState) -> float:
    """Convert Mach number to true airspeed (m/s) at the given state."""
    if mach < 0.0:
        raise ValueError(f"Mach number must be non-negative, got {mach}")
    return mach * state.speed_of_sound_ms


def dynamic_pressure(density_kg_m3: float, speed_ms: float) -> float:
    """Freestream dynamic pressure q = 0.5 * rho * V^2 (Pa)."""
    return 0.5 * density_kg_m3 * speed_ms * speed_ms


def reynolds_number(state: AtmosphereState, speed_ms: float, length_m: float) -> float:
    """Reynolds number based on a reference length.

    Parameters
    ----------
    state:
        Freestream thermodynamic state.
    speed_ms:
        True airspeed (m/s).
    length_m:
        Reference length (m), typically the body length ``L_ref``.
    """
    if length_m <= 0.0:
        raise ValueError(f"reference length must be positive, got {length_m} m")
    return state.density_kg_m3 * speed_ms * length_m / state.viscosity_pa_s


def total_pressure(static_pressure_pa: float, mach: float) -> float:
    """Isentropic stagnation pressure for a perfect gas (Pa)."""
    return static_pressure_pa * (1.0 + 0.5 * (GAMMA_AIR - 1.0) * mach * mach) ** (
        GAMMA_AIR / (GAMMA_AIR - 1.0)
    )


def total_temperature(static_temperature_k: float, mach: float) -> float:
    """Isentropic stagnation temperature for a perfect gas (K)."""
    return static_temperature_k * (1.0 + 0.5 * (GAMMA_AIR - 1.0) * mach * mach)


def skin_friction_coefficient(reynolds_x: float) -> float:
    """Local turbulent skin-friction coefficient on a smooth flat plate.

    Uses the 1/7-power (Prandtl-Schlichting) correlation
    ``cf = 0.0592 * Re_x^(-1/5)``, valid for ``5e5 < Re_x < 1e7`` and used
    here only to size the first wall-normal cell. Below the transition
    Reynolds number the laminar Blasius relation is used instead.
    """
    if reynolds_x <= 0.0:
        raise ValueError(f"Reynolds number must be positive, got {reynolds_x}")
    if reynolds_x < 5.0e5:
        # Laminar Blasius flat plate.
        return 0.664 / math.sqrt(reynolds_x)
    return 0.0592 * reynolds_x ** (-0.2)


def first_cell_height(
    state: AtmosphereState,
    speed_ms: float,
    reference_length_m: float,
    target_yplus: float = 45.0,
) -> float:
    """Wall-normal height of the first cell for a target y+.

    The first cell *centroid* sits at ``y = y+ * nu / u_tau``; because SU2
    stores the solution at cell centroids while the mesh stores cell heights,
    the returned value is twice that distance so the centroid of a prism of
    this height lands on the requested y+.

    Parameters
    ----------
    state:
        Freestream thermodynamic state.
    speed_ms:
        True airspeed (m/s).
    reference_length_m:
        Reference length used for the Reynolds number (m).
    target_yplus:
        Desired y+ at the first cell centroid. Defaults to 45, the centre of
        the 30-60 wall-function band.

    Returns
    -------
    float
        First-layer cell height in metres.
    """
    if speed_ms <= 0.0:
        raise ValueError(f"speed must be positive, got {speed_ms} m/s")
    if not YPLUS_WALL_FUNCTION_MIN <= target_yplus <= YPLUS_WALL_FUNCTION_MAX:
        raise ValueError(
            f"target y+ {target_yplus} outside wall-function band "
            f"[{YPLUS_WALL_FUNCTION_MIN}, {YPLUS_WALL_FUNCTION_MAX}]"
        )

    re_l = reynolds_number(state, speed_ms, reference_length_m)
    cf = skin_friction_coefficient(re_l)
    tau_wall = 0.5 * cf * state.density_kg_m3 * speed_ms * speed_ms
    u_tau = math.sqrt(tau_wall / state.density_kg_m3)
    centroid_distance = target_yplus * state.kinematic_viscosity_m2_s / u_tau
    return 2.0 * centroid_distance


def yplus_from_first_cell_height(
    state: AtmosphereState,
    speed_ms: float,
    reference_length_m: float,
    cell_height_m: float,
) -> float:
    """Inverse of :func:`first_cell_height` -- the y+ a given cell height yields."""
    if cell_height_m <= 0.0:
        raise ValueError(f"cell height must be positive, got {cell_height_m} m")
    re_l = reynolds_number(state, speed_ms, reference_length_m)
    cf = skin_friction_coefficient(re_l)
    tau_wall = 0.5 * cf * state.density_kg_m3 * speed_ms * speed_ms
    u_tau = math.sqrt(tau_wall / state.density_kg_m3)
    return 0.5 * cell_height_m * u_tau / state.kinematic_viscosity_m2_s


def boundary_layer_thickness(
    state: AtmosphereState, speed_ms: float, reference_length_m: float
) -> float:
    """Turbulent boundary-layer thickness at the reference length (m).

    ``delta = 0.37 * L * Re_L^(-1/5)``; used to size the total extrusion
    height of the prism layer stack.
    """
    re_l = reynolds_number(state, speed_ms, reference_length_m)
    return 0.37 * reference_length_m * re_l ** (-0.2)


def prism_stack_height(first_height_m: float, layers: int, growth_rate: float) -> float:
    """Total height of a geometrically growing prism stack (m)."""
    if layers < 1:
        raise ValueError(f"layer count must be >= 1, got {layers}")
    if growth_rate < 1.0:
        raise ValueError(f"growth rate must be >= 1.0, got {growth_rate}")
    if growth_rate == 1.0:
        return first_height_m * layers
    return first_height_m * (growth_rate**layers - 1.0) / (growth_rate - 1.0)


def celsius_to_kelvin(celsius: float) -> float:
    """Convert degrees Celsius to kelvin."""
    return celsius + 273.15


def kelvin_to_celsius(kelvin: float) -> float:
    """Convert kelvin to degrees Celsius."""
    return kelvin - 273.15


def air_gas_constant() -> float:
    """Specific gas constant of dry air, J/(kg K)."""
    return R_SPECIFIC_AIR
