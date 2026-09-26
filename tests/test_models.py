"""Validation behaviour of the typed parameter core."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from core.models import (
    ANGLE_LIMIT_DEG,
    AxisDirection,
    DomainParams,
    FlowParams,
    GeometryParams,
    HingeAxis,
    MeshParams,
    MeshResolution,
    SimulationTrack,
    SolverParams,
    ThermalParams,
    VelocityType,
    cell_count_band,
    normalize_vector,
)


def test_normalize_vector_produces_unit_length():
    """Normalisation yields a unit vector along the original direction."""
    assert normalize_vector([0.0, 3.0, 4.0]) == pytest.approx((0.0, 0.6, 0.8))
    x, y, z = normalize_vector([-2.0, 0.0, 0.0])
    assert (x, y, z) == pytest.approx((-1.0, 0.0, 0.0))


def test_normalize_vector_rejects_degenerate_input():
    """A zero-length vector cannot define a direction."""
    with pytest.raises(ValueError, match="zero magnitude"):
        normalize_vector([0.0, 0.0, 0.0])


@pytest.mark.parametrize(
    "direction,expected",
    [
        (AxisDirection.PLUS_X, (1.0, 0.0, 0.0)),
        (AxisDirection.MINUS_Y, (0.0, -1.0, 0.0)),
        (AxisDirection.PLUS_Z, (0.0, 0.0, 1.0)),
    ],
)
def test_named_axis_directions(direction, expected):
    """Named axes map to the expected unit vectors."""
    assert direction.to_vector() == expected


def test_geometry_prefers_explicit_vector_and_normalises_it():
    """An arbitrary nose vector overrides the named axis and is normalised."""
    geometry = GeometryParams(
        step_file_path="/models/rocket.step",
        nose_direction=AxisDirection.PLUS_X,
        nose_vector=[0.0, 0.0, 7.0],
    )
    assert geometry.resolved_nose_vector() == pytest.approx((0.0, 0.0, 1.0))
    assert geometry.nose_vector == pytest.approx([0.0, 0.0, 1.0])


def test_geometry_requires_some_direction():
    """Clearing both direction fields is rejected."""
    with pytest.raises(ValidationError, match="nose_direction"):
        GeometryParams(
            step_file_path="/models/rocket.step",
            nose_direction=None,
            nose_vector=None,
        )


def test_geometry_rejects_wrong_length_vectors():
    """Vectors must have exactly three components."""
    with pytest.raises(ValidationError):
        GeometryParams(step_file_path="/m.step", nose_vector=[1.0, 0.0])


def test_unknown_fields_are_rejected():
    """Typos in agent-supplied payloads fail loudly rather than being ignored."""
    with pytest.raises(ValidationError):
        DomainParams(upstream_multipler=5.0)  # deliberate typo


def test_domain_defaults_match_specification():
    """Default envelope is 5 L upstream, 10 L downstream, 5 L radial."""
    domain = DomainParams()
    assert domain.upstream_multiplier == 5.0
    assert domain.downstream_multiplier == 10.0
    assert domain.radial_multiplier == 5.0
    assert domain.shape.value == "cylinder"


def test_flow_mach_and_speed_are_consistent_either_way():
    """Specifying Mach or TAS yields the same pair of derived quantities."""
    by_mach = FlowParams(velocity_type=VelocityType.MACH, velocity_value=2.0)
    by_tas = FlowParams(
        velocity_type=VelocityType.TAS, velocity_value=by_mach.speed_ms()
    )
    assert by_tas.mach() == pytest.approx(2.0)
    assert by_tas.speed_ms() == pytest.approx(by_mach.speed_ms())


def test_flow_angle_envelope_is_enforced():
    """Alpha and beta are clamped to the +/-90 degree envelope."""
    FlowParams(velocity_value=1.0, aoa_deg=45.0)
    FlowParams(velocity_value=1.0, aoa_deg=ANGLE_LIMIT_DEG)
    FlowParams(velocity_value=1.0, sideslip_deg=-ANGLE_LIMIT_DEG)
    with pytest.raises(ValidationError):
        FlowParams(velocity_value=1.0, aoa_deg=ANGLE_LIMIT_DEG + 0.1)
    with pytest.raises(ValidationError):
        FlowParams(velocity_value=1.0, sideslip_deg=-ANGLE_LIMIT_DEG - 0.1)


def test_flow_rejects_mach_above_supported_envelope():
    """Mach numbers beyond 3.5 are outside the validated envelope."""
    with pytest.raises(ValidationError, match="exceeds the supported envelope"):
        FlowParams(velocity_value=4.0)
    # The same numeric value is fine when it means metres per second.
    assert FlowParams(velocity_type=VelocityType.TAS, velocity_value=4.0).mach() < 0.02


def test_flow_explicit_conditions_must_come_in_pairs():
    """Half-specified thermodynamic overrides are rejected."""
    with pytest.raises(ValidationError, match="must be supplied"):
        FlowParams(velocity_value=1.0, static_pressure_pa=90_000.0)
    state = FlowParams(
        velocity_value=1.0,
        altitude_m=None,
        static_pressure_pa=90_000.0,
        static_temperature_k=300.0,
    ).atmosphere()
    assert state.pressure_pa == pytest.approx(90_000.0)
    assert math.isnan(state.altitude_m)


def test_flow_requires_altitude_or_explicit_state():
    """Dropping the altitude without an override leaves the state undefined."""
    with pytest.raises(ValidationError, match="supply either"):
        FlowParams(velocity_value=1.0, altitude_m=None)


def test_explicit_conditions_take_precedence_over_altitude():
    """When both are given, the explicit thermodynamic state wins."""
    flow = FlowParams(
        velocity_value=1.0,
        altitude_m=10_000.0,
        static_pressure_pa=101_325.0,
        static_temperature_k=288.15,
    )
    assert flow.atmosphere().pressure_pa == pytest.approx(101_325.0)


@pytest.mark.parametrize("track", list(SimulationTrack))
@pytest.mark.parametrize("resolution", list(MeshResolution))
def test_every_track_resolution_pair_has_a_band(track, resolution):
    """All preset combinations map to a sane, ordered cell-count band."""
    low, high = cell_count_band(track, resolution)
    assert 0 < low < high
    if track is SimulationTrack.AERODYNAMIC:
        assert low >= 250_000 and high <= 750_000
    else:
        assert low >= 150_000 and high <= 400_000


def test_mesh_boundary_layer_count_is_restricted_to_the_spec_range():
    """The prism stack must stay within the 5-8 layer band."""
    MeshParams(boundary_layers=5)
    MeshParams(boundary_layers=8)
    with pytest.raises(ValidationError):
        MeshParams(boundary_layers=4)
    with pytest.raises(ValidationError):
        MeshParams(boundary_layers=9)


def test_mesh_target_band_follows_track_and_resolution():
    """target_band() reflects the configured track and resolution."""
    params = MeshParams(
        track=SimulationTrack.THERMAL, resolution=MeshResolution.COARSE
    )
    assert params.target_band() == (150_000, 230_000)


def test_hinge_axis_direction_is_normalised():
    """Hinge directions are stored as unit vectors."""
    hinge = HingeAxis(name="fin_a", point=[0.5, 0.0, 0.0], direction=[0.0, 0.0, 9.0])
    assert hinge.unit_direction() == pytest.approx((0.0, 0.0, 1.0))


def test_hinge_axis_rejects_degenerate_direction():
    """A zero-length hinge direction cannot define a rotation axis."""
    with pytest.raises(ValidationError):
        HingeAxis(point=[0.0, 0.0, 0.0], direction=[0.0, 0.0, 0.0])


def test_thermal_defaults_match_the_bmp580_scenario():
    """Defaults encode the 2 m/s, 25 C, 800 W/m^2, 0.1 m tram-roof case."""
    thermal = ThermalParams(
        enclosure_step_path="/models/housing.step", sensor_xyz=[0.0, 0.0, 0.02]
    )
    assert thermal.vehicle_speed_ms == 2.0
    assert thermal.ambient_temp_c == 25.0
    assert thermal.solar_flux_w_m2 == 800.0
    assert thermal.height_above_roof_m == 0.1
    assert thermal.housing_conductivity_w_mk == pytest.approx(0.18)
    assert thermal.sensor_power_w == pytest.approx(1.0e-3)
    assert thermal.ambient_temp_k() == pytest.approx(298.15)


def test_swinbank_sky_temperature_is_below_ambient():
    """The clear-sky radiative temperature must sit below ambient."""
    thermal = ThermalParams(
        enclosure_step_path="/m.step", sensor_xyz=[0.0, 0.0, 0.0], ambient_temp_c=25.0
    )
    sky = thermal.effective_sky_temperature_k()
    assert 250.0 < sky < thermal.ambient_temp_k()


def test_explicit_sky_temperature_overrides_the_correlation():
    """An explicitly supplied sky temperature is used verbatim."""
    thermal = ThermalParams(
        enclosure_step_path="/m.step",
        sensor_xyz=[0.0, 0.0, 0.0],
        sky_temperature_k=240.0,
    )
    assert thermal.effective_sky_temperature_k() == pytest.approx(240.0)


def test_solver_defaults_target_the_reference_machine():
    """Ten MPI ranks and a -5 residual threshold, per the specification."""
    solver = SolverParams()
    assert solver.mpi_ranks == 10
    assert solver.convergence_residual == -5.0
    assert solver.turbulence_model.value == "SST"
    assert solver.limiter == "VENKATAKRISHNAN"


def test_assignment_is_validated_after_construction():
    """validate_assignment keeps GUI slider writes inside the declared bounds."""
    flow = FlowParams(velocity_value=1.0)
    flow.aoa_deg = 10.0
    assert flow.aoa_deg == 10.0
    with pytest.raises(ValidationError):
        flow.aoa_deg = 95.0


def test_models_expose_descriptions_for_agent_schemas():
    """Every field carries a description so MCP schemas are self-documenting."""
    for model in (GeometryParams, DomainParams, MeshParams, FlowParams, ThermalParams):
        schema = model.model_json_schema()
        for name, spec in schema["properties"].items():
            # anyOf-typed optional fields carry the description at the top level.
            assert spec.get("description"), f"{model.__name__}.{name} lacks a description"


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


def test_the_rocket_frame_puts_the_nose_on_plus_z():
    """Solver -X (towards the nose, upstream) is rocket +Z."""
    import numpy as np

    from core.frames import ROCKET_TO_SOLVER, SOLVER_TO_ROCKET, to_rocket, to_solver

    assert to_rocket([-1.0, 0.0, 0.0]) == pytest.approx([0.0, 0.0, 1.0])
    # Lift from a positive angle of attack is solver +Z: rocket +X.
    assert to_rocket([0.0, 0.0, 1.0]) == pytest.approx([1.0, 0.0, 0.0])
    assert to_rocket([0.0, 1.0, 0.0]) == pytest.approx([0.0, 1.0, 0.0])
    # A proper rotation: cross products and moments survive it.
    assert np.linalg.det(SOLVER_TO_ROCKET) == pytest.approx(1.0)
    assert SOLVER_TO_ROCKET @ ROCKET_TO_SOLVER == pytest.approx(np.eye(3))
    vector = [0.3, -1.2, 2.5]
    assert to_solver(to_rocket(vector)) == pytest.approx(vector)


def test_a_hinge_converts_between_frames():
    from core.frames import Frame

    hinge = HingeAxis(name="fin", point=[0.0, 0.05, -0.9], direction=[0, 2, 0],
                      frame=Frame.ROCKET)
    assert hinge.solver_point() == pytest.approx([0.9, 0.05, 0.0])
    assert hinge.solver_direction() == pytest.approx([0.0, 1.0, 0.0])
    solver = hinge.in_frame(Frame.SOLVER)
    assert solver.frame is Frame.SOLVER
    assert solver.in_frame(Frame.ROCKET).point == pytest.approx(hinge.point)


def test_an_old_hinge_without_a_frame_is_in_the_solver_frame():
    """Projects saved before the rocket frame keep their meaning."""
    from core.frames import Frame

    hinge = HingeAxis.model_validate(
        {"name": "fin", "point": [0.9, 0.05, 0.0], "direction": [0, 1, 0]}
    )
    assert hinge.frame is Frame.SOLVER
    assert hinge.solver_point() == pytest.approx([0.9, 0.05, 0.0])


def test_a_bare_axis_name_is_read_as_its_positive_direction():
    """The assistant asked for pitch_axis="X" and was refused a whole round."""
    from core.models import AxisDirection

    assert AxisDirection("X") is AxisDirection.PLUS_X
    assert AxisDirection(" -z ") is AxisDirection.MINUS_Z
    with pytest.raises(ValueError):
        AxisDirection("W")
