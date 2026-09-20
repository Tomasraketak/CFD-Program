"""Similarity-parameter and boundary-layer sizing checks."""

from __future__ import annotations

import pytest

from core.atmosphere import isa_state
from core.units import (
    boundary_layer_thickness,
    dynamic_pressure,
    first_cell_height,
    mach_from_speed,
    prism_stack_height,
    reynolds_number,
    skin_friction_coefficient,
    speed_from_mach,
    total_pressure,
    total_temperature,
    yplus_from_first_cell_height,
)

SEA_LEVEL = isa_state(0.0)


def test_mach_speed_round_trip():
    """Mach -> TAS -> Mach is lossless across the supported envelope."""
    for mach in (0.2, 0.8, 1.0, 2.0, 3.5):
        speed = speed_from_mach(mach, SEA_LEVEL)
        assert mach_from_speed(speed, SEA_LEVEL) == pytest.approx(mach)


def test_mach_one_equals_speed_of_sound():
    """Mach 1 is exactly the local speed of sound."""
    assert speed_from_mach(1.0, SEA_LEVEL) == pytest.approx(SEA_LEVEL.speed_of_sound_ms)


def test_dynamic_pressure_matches_definition():
    """q = 0.5 rho V^2."""
    assert dynamic_pressure(1.225, 100.0) == pytest.approx(6125.0)


def test_reynolds_number_order_of_magnitude():
    """A 1 m body at Mach 2 sea level sits around Re = 4.7e7."""
    speed = speed_from_mach(2.0, SEA_LEVEL)
    assert reynolds_number(SEA_LEVEL, speed, 1.0) == pytest.approx(4.66e7, rel=0.02)


def test_isentropic_stagnation_relations():
    """Stagnation ratios match the textbook values at Mach 1 and Mach 2."""
    # At M=1, p0/p = 1.8929 and T0/T = 1.2 for gamma = 1.4.
    assert total_pressure(1.0, 1.0) == pytest.approx(1.8929, rel=1e-4)
    assert total_temperature(1.0, 1.0) == pytest.approx(1.2, rel=1e-9)
    # At M=2, p0/p = 7.8241 and T0/T = 1.8.
    assert total_pressure(1.0, 2.0) == pytest.approx(7.8241, rel=1e-4)
    assert total_temperature(1.0, 2.0) == pytest.approx(1.8, rel=1e-9)
    # At M=0 the stagnation state is the static state.
    assert total_pressure(101_325.0, 0.0) == pytest.approx(101_325.0)


def test_skin_friction_switches_to_laminar_below_transition():
    """The correlation uses Blasius below Re = 5e5 and the power law above."""
    assert skin_friction_coefficient(1.0e5) == pytest.approx(0.664 / 1.0e5**0.5)
    assert skin_friction_coefficient(1.0e7) == pytest.approx(0.0592 * 1.0e7**-0.2)


def test_skin_friction_decreases_with_reynolds():
    """Skin friction falls monotonically with Reynolds number."""
    values = [skin_friction_coefficient(re) for re in (1e6, 1e7, 1e8)]
    assert values == sorted(values, reverse=True)


@pytest.mark.parametrize("target_yplus", [30.0, 45.0, 60.0])
@pytest.mark.parametrize("mach", [0.2, 1.5, 3.0])
def test_first_cell_height_round_trips_to_target_yplus(target_yplus, mach):
    """Sizing a cell for a y+ and measuring it back returns that y+."""
    speed = speed_from_mach(mach, SEA_LEVEL)
    height = first_cell_height(SEA_LEVEL, speed, 1.0, target_yplus)
    assert height > 0.0
    recovered = yplus_from_first_cell_height(SEA_LEVEL, speed, 1.0, height)
    assert recovered == pytest.approx(target_yplus, rel=1e-9)


def test_first_cell_height_shrinks_with_speed():
    """Faster flow needs a thinner first cell to hold the same y+."""
    slow = first_cell_height(SEA_LEVEL, 50.0, 1.0, 45.0)
    fast = first_cell_height(SEA_LEVEL, 500.0, 1.0, 45.0)
    assert fast < slow


def test_first_cell_is_far_thinner_than_the_boundary_layer():
    """The wall cell must be a small fraction of the boundary-layer thickness."""
    speed = speed_from_mach(2.0, SEA_LEVEL)
    height = first_cell_height(SEA_LEVEL, speed, 1.0, 45.0)
    delta = boundary_layer_thickness(SEA_LEVEL, speed, 1.0)
    assert height < 0.05 * delta


def test_yplus_outside_wall_function_band_is_rejected():
    """Requesting a y+ that invalidates wall functions raises."""
    with pytest.raises(ValueError, match="outside wall-function band"):
        first_cell_height(SEA_LEVEL, 100.0, 1.0, 1.0)
    with pytest.raises(ValueError):
        first_cell_height(SEA_LEVEL, 100.0, 1.0, 5000.0)


def test_prism_stack_height_geometric_series():
    """The stack height follows the geometric-series sum."""
    assert prism_stack_height(1.0, 4, 2.0) == pytest.approx(15.0)
    # A unit growth rate degenerates to a uniform stack.
    assert prism_stack_height(0.5, 6, 1.0) == pytest.approx(3.0)


def test_prism_stack_rejects_shrinking_layers():
    """A growth rate below one would make layers shrink away from the wall."""
    with pytest.raises(ValueError):
        prism_stack_height(1e-5, 7, 0.9)
    with pytest.raises(ValueError):
        prism_stack_height(1e-5, 0, 1.2)


def test_non_physical_inputs_are_rejected():
    """Negative speeds and lengths raise instead of silently producing nonsense."""
    with pytest.raises(ValueError):
        speed_from_mach(-1.0, SEA_LEVEL)
    with pytest.raises(ValueError):
        mach_from_speed(-1.0, SEA_LEVEL)
    with pytest.raises(ValueError):
        reynolds_number(SEA_LEVEL, 100.0, 0.0)
    with pytest.raises(ValueError):
        first_cell_height(SEA_LEVEL, 0.0, 1.0)
