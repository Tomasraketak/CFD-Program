"""International Standard Atmosphere (ISA-1976) up to 32 km.

Provides freestream static conditions (pressure, temperature, density,
dynamic viscosity, speed of sound) required to non-dimensionalise the SU2
freestream definition for the aerodynamic track.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Physical constants (SI)
R_SPECIFIC_AIR = 287.052_87  # J/(kg K)
GAMMA_AIR = 1.4  # ratio of specific heats [-]
G0 = 9.806_65  # m/s^2, standard gravity
CP_AIR = GAMMA_AIR * R_SPECIFIC_AIR / (GAMMA_AIR - 1.0)  # J/(kg K)

# Sutherland's law constants for air
SUTHERLAND_MU_REF = 1.716e-5  # Pa s
SUTHERLAND_T_REF = 273.15  # K
SUTHERLAND_S = 110.4  # K

# Sea-level reference state
T0_SEA_LEVEL = 288.15  # K
P0_SEA_LEVEL = 101_325.0  # Pa
RHO0_SEA_LEVEL = P0_SEA_LEVEL / (R_SPECIFIC_AIR * T0_SEA_LEVEL)  # kg/m^3

# ISA layers: (base geopotential altitude [m], lapse rate [K/m])
# Valid from -610 m to 32 000 m.
_ISA_LAYERS: tuple[tuple[float, float], ...] = (
    (0.0, -0.0065),  # troposphere
    (11_000.0, 0.0),  # tropopause
    (20_000.0, 0.001),  # stratosphere I
    (32_000.0, 0.0028),  # stratosphere II (upper bound of this model)
)

ALTITUDE_MIN_M = -610.0
ALTITUDE_MAX_M = 32_000.0

# Earth radius used for geometric -> geopotential altitude conversion.
EARTH_RADIUS_M = 6_356_766.0


@dataclass(frozen=True)
class AtmosphereState:
    """Freestream thermodynamic state of still air.

    Attributes
    ----------
    altitude_m:
        Geopotential altitude the state was evaluated at (m). ``nan`` when the
        state was constructed directly from explicit pressure/temperature.
    temperature_k:
        Static temperature (K).
    pressure_pa:
        Static pressure (Pa).
    density_kg_m3:
        Static density (kg/m^3).
    viscosity_pa_s:
        Dynamic viscosity from Sutherland's law (Pa s).
    speed_of_sound_ms:
        Speed of sound (m/s).
    """

    altitude_m: float
    temperature_k: float
    pressure_pa: float
    density_kg_m3: float
    viscosity_pa_s: float
    speed_of_sound_ms: float

    @property
    def kinematic_viscosity_m2_s(self) -> float:
        """Kinematic viscosity nu = mu / rho (m^2/s)."""
        return self.viscosity_pa_s / self.density_kg_m3

    def as_dict(self) -> dict[str, float]:
        """Return the state as a plain JSON-serialisable dictionary."""
        return {
            "altitude_m": self.altitude_m,
            "temperature_k": self.temperature_k,
            "pressure_pa": self.pressure_pa,
            "density_kg_m3": self.density_kg_m3,
            "viscosity_pa_s": self.viscosity_pa_s,
            "speed_of_sound_ms": self.speed_of_sound_ms,
        }


def sutherland_viscosity(temperature_k: float) -> float:
    """Dynamic viscosity of air from Sutherland's law.

    Parameters
    ----------
    temperature_k:
        Static temperature in kelvin. Must be strictly positive.

    Returns
    -------
    float
        Dynamic viscosity in Pa s.
    """
    if temperature_k <= 0.0:
        raise ValueError(f"temperature must be positive, got {temperature_k} K")
    return (
        SUTHERLAND_MU_REF
        * (temperature_k / SUTHERLAND_T_REF) ** 1.5
        * (SUTHERLAND_T_REF + SUTHERLAND_S)
        / (temperature_k + SUTHERLAND_S)
    )


def speed_of_sound(temperature_k: float) -> float:
    """Speed of sound in air: a = sqrt(gamma * R * T) (m/s)."""
    if temperature_k <= 0.0:
        raise ValueError(f"temperature must be positive, got {temperature_k} K")
    return math.sqrt(GAMMA_AIR * R_SPECIFIC_AIR * temperature_k)


def geometric_to_geopotential(altitude_m: float) -> float:
    """Convert geometric altitude to geopotential altitude (m)."""
    return EARTH_RADIUS_M * altitude_m / (EARTH_RADIUS_M + altitude_m)


def isa_state(altitude_m: float, geometric: bool = False) -> AtmosphereState:
    """Evaluate the ISA-1976 standard atmosphere at an altitude.

    The ISA layer boundaries (11 km, 20 km, 32 km) are defined in
    *geopotential* altitude, which is how the standard tables are indexed, so
    that is this function's default input convention. Pass
    ``geometric=True`` to supply a geometric (true) altitude instead; it is
    converted internally. The two differ by less than 0.2% below 12 km.

    Parameters
    ----------
    altitude_m:
        Altitude in metres, within ``[-610, 32000]``.
    geometric:
        When True, ``altitude_m`` is interpreted as geometric altitude and
        converted to geopotential before evaluation.

    Returns
    -------
    AtmosphereState
        Static pressure, temperature, density, viscosity and speed of sound.

    Raises
    ------
    ValueError
        If the altitude falls outside the validity range of the model.
    """
    if not ALTITUDE_MIN_M <= altitude_m <= ALTITUDE_MAX_M:
        raise ValueError(
            f"altitude {altitude_m} m outside ISA model range "
            f"[{ALTITUDE_MIN_M}, {ALTITUDE_MAX_M}] m"
        )

    h = geometric_to_geopotential(altitude_m) if geometric else altitude_m

    temperature = T0_SEA_LEVEL
    pressure = P0_SEA_LEVEL

    for index, (base_h, lapse) in enumerate(_ISA_LAYERS):
        # Height spanned inside this layer.
        next_h = _ISA_LAYERS[index + 1][0] if index + 1 < len(_ISA_LAYERS) else math.inf
        top_h = min(h, next_h)
        if top_h <= base_h:
            break
        dh = top_h - base_h

        if lapse == 0.0:
            pressure *= math.exp(-G0 * dh / (R_SPECIFIC_AIR * temperature))
        else:
            t_top = temperature + lapse * dh
            pressure *= (t_top / temperature) ** (-G0 / (lapse * R_SPECIFIC_AIR))
            temperature = t_top

        if h <= next_h:
            break

    density = pressure / (R_SPECIFIC_AIR * temperature)
    return AtmosphereState(
        altitude_m=altitude_m,
        temperature_k=temperature,
        pressure_pa=pressure,
        density_kg_m3=density,
        viscosity_pa_s=sutherland_viscosity(temperature),
        speed_of_sound_ms=speed_of_sound(temperature),
    )


def state_from_pressure_temperature(
    pressure_pa: float, temperature_k: float
) -> AtmosphereState:
    """Build an :class:`AtmosphereState` from explicit static conditions.

    Used when the operator specifies freestream pressure and temperature
    directly instead of an altitude.
    """
    if pressure_pa <= 0.0:
        raise ValueError(f"pressure must be positive, got {pressure_pa} Pa")
    if temperature_k <= 0.0:
        raise ValueError(f"temperature must be positive, got {temperature_k} K")
    return AtmosphereState(
        altitude_m=math.nan,
        temperature_k=temperature_k,
        pressure_pa=pressure_pa,
        density_kg_m3=pressure_pa / (R_SPECIFIC_AIR * temperature_k),
        viscosity_pa_s=sutherland_viscosity(temperature_k),
        speed_of_sound_ms=speed_of_sound(temperature_k),
    )
