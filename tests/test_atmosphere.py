"""ISA atmosphere checks against the published ISA-1976 tables."""

from __future__ import annotations

import math

import pytest

from core.atmosphere import (
    GAMMA_AIR,
    R_SPECIFIC_AIR,
    isa_state,
    speed_of_sound,
    state_from_pressure_temperature,
    sutherland_viscosity,
)

# (altitude m, T K, p Pa, rho kg/m^3) from the ISA-1976 standard tables.
ISA_TABLE = [
    (0.0, 288.15, 101_325.0, 1.2250),
    (1_000.0, 281.65, 89_874.6, 1.1117),
    (5_000.0, 255.65, 54_019.9, 0.73612),
    (11_000.0, 216.65, 22_632.1, 0.36392),
    (15_000.0, 216.65, 12_044.6, 0.19367),
    (20_000.0, 216.65, 5_474.9, 0.08803),
]


@pytest.mark.parametrize("altitude,temperature,pressure,density", ISA_TABLE)
def test_isa_matches_published_table(altitude, temperature, pressure, density):
    """Temperature, pressure and density track the standard tables to 0.5%."""
    state = isa_state(altitude)
    assert state.temperature_k == pytest.approx(temperature, rel=5e-3)
    assert state.pressure_pa == pytest.approx(pressure, rel=5e-3)
    assert state.density_kg_m3 == pytest.approx(density, rel=5e-3)


def test_isa_is_monotonic_in_pressure_and_density():
    """Pressure and density decrease monotonically with altitude."""
    altitudes = range(0, 32_001, 1_000)
    states = [isa_state(float(h)) for h in altitudes]
    pressures = [s.pressure_pa for s in states]
    densities = [s.density_kg_m3 for s in states]
    assert pressures == sorted(pressures, reverse=True)
    assert densities == sorted(densities, reverse=True)


def test_isothermal_layer_holds_temperature_constant():
    """The 11-20 km tropopause layer is isothermal at 216.65 K."""
    for altitude in (11_000.0, 15_000.0, 20_000.0):
        assert isa_state(altitude).temperature_k == pytest.approx(216.65, abs=0.05)


def test_ideal_gas_law_is_consistent():
    """Density agrees with p = rho * R * T at every altitude."""
    for altitude in (0.0, 8_000.0, 25_000.0):
        state = isa_state(altitude)
        assert state.pressure_pa == pytest.approx(
            state.density_kg_m3 * R_SPECIFIC_AIR * state.temperature_k, rel=1e-9
        )


def test_speed_of_sound_at_sea_level():
    """Sea-level speed of sound is the textbook 340.3 m/s."""
    assert speed_of_sound(288.15) == pytest.approx(340.29, abs=0.05)
    assert isa_state(0.0).speed_of_sound_ms == pytest.approx(
        math.sqrt(GAMMA_AIR * R_SPECIFIC_AIR * 288.15)
    )


def test_sutherland_viscosity_at_sea_level():
    """Sea-level dynamic viscosity is ~1.789e-5 Pa s."""
    assert sutherland_viscosity(288.15) == pytest.approx(1.789e-5, rel=5e-3)


def test_altitude_outside_model_range_is_rejected():
    """Altitudes beyond the model validity window raise."""
    with pytest.raises(ValueError, match="outside ISA model range"):
        isa_state(40_000.0)
    with pytest.raises(ValueError):
        isa_state(-5_000.0)


def test_explicit_state_construction():
    """An explicitly specified state honours the ideal gas law."""
    state = state_from_pressure_temperature(90_000.0, 300.0)
    assert state.density_kg_m3 == pytest.approx(90_000.0 / (R_SPECIFIC_AIR * 300.0))
    assert math.isnan(state.altitude_m)


def test_non_physical_inputs_are_rejected():
    """Zero or negative pressure/temperature raise rather than producing NaN."""
    with pytest.raises(ValueError):
        state_from_pressure_temperature(0.0, 300.0)
    with pytest.raises(ValueError):
        state_from_pressure_temperature(1e5, -1.0)
    with pytest.raises(ValueError):
        sutherland_viscosity(0.0)
