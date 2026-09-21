"""BMP580 sensor microclimate: conjugate heat transfer.

The lumped model is checked for physical correctness rather than against
fixed numbers: energy balances must close, and the sensor bias must respond in
the right direction to sun, speed, height and surface finish. Those trends are
what an operator relies on when siting the sensor, and they are what a coding
error would break.
"""

from __future__ import annotations

import math

import pytest

from backend.runner import FakeRunner
from backend.su2_config import parse_config
from backend.thermal_solver import (
    PRANDTL_AIR,
    STEFAN_BOLTZMANN,
    LumpedGeometry,
    LumpedThermalModel,
    ThermalSolverError,
    air_conductivity,
    build_thermal_config,
    convection_coefficient,
    estimate_sensor_bias,
    flat_plate_nusselt,
    radiation_coefficient,
    roof_flux_w_m2,
    run_thermal_case,
    tube_nusselt,
    ThermalConfigContext,
)
from core.models import SolverParams, ThermalParams
from core.units import kelvin_to_celsius

SENSOR_XYZ = [0.03, 0.02, 0.015]


def make_params(**overrides) -> ThermalParams:
    """The specified tram-roof scenario, with optional overrides."""
    return ThermalParams(
        enclosure_step_path="/models/housing.step",
        sensor_xyz=SENSOR_XYZ,
        **overrides,
    )


# ---------------------------------------------------------------------------
# Correlations
# ---------------------------------------------------------------------------


def test_flat_plate_nusselt_switches_at_transition():
    """Blasius below Re = 5e5, Colburn above."""
    assert flat_plate_nusselt(1.0e4) == pytest.approx(
        0.664 * math.sqrt(1.0e4) * PRANDTL_AIR ** (1 / 3)
    )
    assert flat_plate_nusselt(1.0e6) == pytest.approx(
        0.037 * 1.0e6**0.8 * PRANDTL_AIR ** (1 / 3)
    )
    assert flat_plate_nusselt(0.0) == 0.0


def test_nusselt_increases_with_reynolds():
    """Faster flow transfers more heat."""
    values = [flat_plate_nusselt(re) for re in (1e3, 1e4, 1e5, 1e6, 1e7)]
    assert values == sorted(values)


def test_tube_nusselt_is_constant_in_laminar_flow():
    """Fully developed laminar tube flow has Nu = 3.66."""
    assert tube_nusselt(500.0) == pytest.approx(3.66)
    assert tube_nusselt(1.0e4) > 3.66


def test_radiation_coefficient_reproduces_the_fourth_power_law():
    """The linearisation is exact, not an approximation.

    h_r * (Ts - Tsky) must equal e*sigma*(Ts^4 - Tsky^4); this identity is
    what allows a fourth-power law to be expressed as SU2's linear wall flux.
    """
    surface, sky, emissivity = 330.0, 250.0, 0.9
    linearised = radiation_coefficient(surface, sky, emissivity) * (surface - sky)
    exact = emissivity * STEFAN_BOLTZMANN * (surface**4 - sky**4)
    assert linearised == pytest.approx(exact, rel=1e-12)


def test_air_conductivity_is_physical():
    """Air conductivity near room temperature is about 0.026 W/(m K)."""
    assert air_conductivity(300.0) == pytest.approx(0.026, abs=0.002)


def test_convection_coefficient_has_a_natural_convection_floor():
    """A stationary vehicle must not give zero heat transfer."""
    assert convection_coefficient(0.0, 1.0, 300.0, 1.2, 1.8e-5) >= 5.0


def test_convection_coefficient_grows_with_speed():
    """Faster airflow cools the roof harder."""
    slow = convection_coefficient(1.0, 1.0, 300.0, 1.2, 1.8e-5)
    fast = convection_coefficient(20.0, 1.0, 300.0, 1.2, 1.8e-5)
    assert fast > slow


# ---------------------------------------------------------------------------
# Roof energy balance
# ---------------------------------------------------------------------------


def test_roof_flux_balances_absorbed_solar_against_emission():
    """The roof boundary flux follows q = aG - e*sigma*(Tw^4 - Tsky^4)."""
    params = make_params(solar_flux_w_m2=800.0)
    sky = params.effective_sky_temperature_k()
    wall = 330.0
    expected = params.roof_solar_absorptivity * 800.0 - (
        params.roof_emissivity * STEFAN_BOLTZMANN * (wall**4 - sky**4)
    )
    assert roof_flux_w_m2(params, wall) == pytest.approx(expected)


def test_roof_flux_falls_as_the_wall_heats_up():
    """A hotter roof radiates more, so net absorbed flux drops."""
    params = make_params()
    assert roof_flux_w_m2(params, 350.0) < roof_flux_w_m2(params, 300.0)


def test_roof_energy_balance_closes():
    """At the solved roof temperature, inflow equals outflow.

    This is the check that the fixed-point iteration actually converged
    rather than simply stopping.
    """
    params = make_params()
    model = LumpedThermalModel(params)
    result = model.solve()

    ambient = params.ambient_temp_k()
    sky = params.effective_sky_temperature_k()
    absorbed = params.roof_solar_absorptivity * params.solar_flux_w_m2
    convected = result.roof_convection_coefficient * (result.roof_temp_k - ambient)
    radiated = (
        params.roof_emissivity
        * STEFAN_BOLTZMANN
        * (result.roof_temp_k**4 - sky**4)
    )
    assert absorbed == pytest.approx(convected + radiated, rel=1e-3)


# ---------------------------------------------------------------------------
# The reference scenario
# ---------------------------------------------------------------------------


def test_reference_scenario_is_physically_plausible():
    """2 m/s, 25 C, 800 W/m^2, 0.1 m above a tram roof.

    A dark roof under strong sun with only 2 m/s of cooling airflow sits well
    above ambient but far below anything that would melt it, and the sensor
    reads slightly high.
    """
    result = estimate_sensor_bias(make_params())

    roof_c = kelvin_to_celsius(result.roof_temp_k)
    assert 40.0 < roof_c < 90.0, f"roof at {roof_c:.1f} C is not credible"

    # The sensor must sit between ambient and the housing that surrounds it.
    assert result.ambient_temp_k <= result.sensor_temp_k <= result.housing_temp_k + 0.5
    assert 0.0 < result.delta_t_error_k < 6.0


def test_ram_air_flushes_the_chamber():
    """Vehicle motion drives a real mass flow through the sampling tube."""
    result = estimate_sensor_bias(make_params(vehicle_speed_ms=2.0))
    # Losses mean the bore velocity is below the vehicle speed but not zero.
    assert 0.0 < result.tube_velocity_ms < 2.0
    assert result.tube_mass_flow_kg_s > 0.0
    # Several chamber volumes per second at this flow.
    assert result.tube_reynolds > 100.0


def test_self_heating_is_negligible_at_one_milliwatt():
    """1 mW into a flushed chamber cannot move the reading measurably.

    Confirming this matters: it tells the operator the error is environmental,
    not the sensor heating itself.
    """
    result = estimate_sensor_bias(make_params())
    assert result.self_heating_rise_k < 0.05
    # The bias is dominated by coupling to the warm housing walls.
    assert result.wall_coupling_rise_k > 10.0 * result.self_heating_rise_k


def test_intake_height_is_compared_against_the_roof_thermal_layer():
    """Whether the intake samples ambient air is the key siting question."""
    high = estimate_sensor_bias(make_params(height_above_roof_m=0.1))
    low = estimate_sensor_bias(make_params(height_above_roof_m=0.005))

    assert not high.intake_in_roof_plume
    assert low.intake_in_roof_plume
    assert low.delta_t_error_k > high.delta_t_error_k


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------


def test_bias_grows_with_solar_flux():
    """More sun, hotter roof and housing, larger error."""
    biases = [
        estimate_sensor_bias(make_params(solar_flux_w_m2=flux)).delta_t_error_k
        for flux in (0.0, 200.0, 500.0, 800.0, 1100.0)
    ]
    assert biases == sorted(biases)


def test_night_time_bias_is_negative():
    """Without sun, radiative cooling to the sky pulls the reading below ambient.

    Getting this sign right matters: a model that only ever reads high would
    hide night-time cold bias.
    """
    result = estimate_sensor_bias(make_params(solar_flux_w_m2=0.0))
    assert result.delta_t_error_k < 0.0
    assert result.roof_temp_k < result.ambient_temp_k


def test_bias_falls_as_the_vehicle_speeds_up():
    """Faster flushing brings the reading back towards ambient."""
    biases = [
        estimate_sensor_bias(make_params(vehicle_speed_ms=v)).delta_t_error_k
        for v in (1.0, 2.0, 5.0, 10.0, 20.0)
    ]
    assert biases == sorted(biases, reverse=True)


def test_stationary_case_is_the_worst_case():
    """With no ram air the chamber equilibrates with the warm housing."""
    stationary = estimate_sensor_bias(make_params(vehicle_speed_ms=0.0))
    moving = estimate_sensor_bias(make_params(vehicle_speed_ms=2.0))
    assert stationary.delta_t_error_k > moving.delta_t_error_k
    assert stationary.tube_mass_flow_kg_s == pytest.approx(0.0)


def test_a_reflective_housing_reduces_the_error():
    """Surface finish is the cheapest fix available to the operator."""
    white = estimate_sensor_bias(make_params(housing_solar_absorptivity=0.1))
    black = estimate_sensor_bias(make_params(housing_solar_absorptivity=0.95))
    assert white.delta_t_error_k < black.delta_t_error_k


def test_a_reflective_roof_cools_everything():
    """Roof absorptivity drives the whole microclimate."""
    shiny = estimate_sensor_bias(make_params(roof_solar_absorptivity=0.2))
    dark = estimate_sensor_bias(make_params(roof_solar_absorptivity=0.95))
    assert shiny.roof_temp_k < dark.roof_temp_k


def test_ambient_temperature_shifts_everything_together():
    """Raising ambient raises every temperature, leaving a similar bias."""
    cool = estimate_sensor_bias(make_params(ambient_temp_c=5.0))
    warm = estimate_sensor_bias(make_params(ambient_temp_c=35.0))
    assert warm.sensor_temp_k > cool.sensor_temp_k
    assert warm.roof_temp_k > cool.roof_temp_k


def test_a_more_conductive_housing_does_not_break_the_solve():
    """An aluminium-like housing is accepted and stays physical."""
    result = estimate_sensor_bias(
        make_params(housing_conductivity_w_mk=200.0)
    )
    assert math.isfinite(result.delta_t_error_k)
    assert result.sensor_temp_k > 0.0


def test_solution_is_converged_and_repeatable():
    """More iterations must not move the answer."""
    params = make_params()
    short = LumpedThermalModel(params).solve(iterations=40)
    long = LumpedThermalModel(params).solve(iterations=400)
    assert short.sensor_temp_k == pytest.approx(long.sensor_temp_k, abs=1e-6)
    assert short.roof_temp_k == pytest.approx(long.roof_temp_k, abs=1e-6)


def test_result_is_json_serialisable():
    """The summary embeds into MCP responses."""
    payload = estimate_sensor_bias(make_params()).as_dict()
    assert "delta_t_error_k" in payload
    assert payload["sensor_temp_c"] == pytest.approx(
        kelvin_to_celsius(payload["sensor_temp_k"])
    )


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def test_geometry_derives_areas_and_volumes():
    """Chamber dimensions follow from the external size and wall thickness."""
    geometry = LumpedGeometry()
    assert geometry.chamber_volume_m3 > 0.0
    assert geometry.chamber_wall_area_m2 > 0.0
    assert geometry.tube_area_m2 == pytest.approx(
        math.pi * 0.25 * geometry.tube_diameter_m**2
    )


def test_a_larger_chamber_couples_less_strongly_per_unit_flow():
    """Geometry is exposed so enclosure design can be traded off."""
    small = estimate_sensor_bias(
        make_params(),
    )
    big = LumpedThermalModel(
        make_params(), LumpedGeometry(tube_diameter_m=0.016)
    ).solve()
    # A wider intake flushes harder and pulls the reading towards ambient.
    assert big.tube_mass_flow_kg_s > small.tube_mass_flow_kg_s
    assert big.delta_t_error_k < small.delta_t_error_k


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_thermal_config_declares_a_multizone_conjugate_problem():
    """The conjugate setup needs the multiphysics driver and CHT interfaces."""
    text = build_thermal_config(
        make_params(), SolverParams(), ThermalConfigContext(mesh_filename="m.su2")
    )
    settings = parse_config(text)
    assert settings["SOLVER"] in ("MULTIPHYSICS", "INC_RANS")
    assert "MARKER_CHT_INTERFACE" in settings
    assert "CHT_INTERFACE" in settings["MARKER_CHT_INTERFACE"]
    assert settings["MESH_FILENAME"] == "m.su2"
    assert settings["MULTIZONE_MESH"] == "YES"


def test_thermal_config_carries_the_solid_material():
    """PLA/PETG properties reach the solid zone."""
    params = make_params(housing_conductivity_w_mk=0.18)
    settings = parse_config(
        build_thermal_config(
            params, SolverParams(), ThermalConfigContext(mesh_filename="m.su2")
        )
    )
    assert float(settings["SOLID_THERMAL_CONDUCTIVITY"]) == pytest.approx(0.18)
    assert float(settings["SOLID_DENSITY"]) == pytest.approx(
        params.housing_density_kg_m3
    )


def test_thermal_config_applies_the_linearised_roof_flux():
    """The solar-driven roof appears as a net wall flux."""
    params = make_params(solar_flux_w_m2=800.0)
    settings = parse_config(
        build_thermal_config(
            params,
            SolverParams(),
            ThermalConfigContext(mesh_filename="m.su2"),
            roof_wall_temperature_k=330.0,
        )
    )
    marker = settings["MARKER_HEATFLUX"]
    assert "ROOF" in marker
    flux = float(marker.split(",")[1].strip(" )"))
    assert flux == pytest.approx(roof_flux_w_m2(params, 330.0), rel=1e-6)
    # Under strong sun the roof is a net heat source.
    assert flux > 0.0


def test_thermal_config_enables_the_energy_equation():
    """A conjugate solve without the energy equation would be meaningless."""
    settings = parse_config(
        build_thermal_config(
            make_params(), SolverParams(), ThermalConfigContext(mesh_filename="m.su2")
        )
    )
    assert settings["INC_ENERGY_EQUATION"] == "YES"
    assert float(settings["INC_TEMPERATURE_INIT"]) == pytest.approx(298.15)


def test_thermal_config_sets_the_vehicle_speed():
    """Ram air comes from the initialised freestream velocity."""
    settings = parse_config(
        build_thermal_config(
            make_params(vehicle_speed_ms=2.0),
            SolverParams(),
            ThermalConfigContext(mesh_filename="m.su2"),
        )
    )
    assert "2.0" in settings["INC_VELOCITY_INIT"]


# ---------------------------------------------------------------------------
# Running a case
# ---------------------------------------------------------------------------


@pytest.fixture
def thermal_mesh(tmp_path):
    """A stand-in multizone mesh file."""
    path = tmp_path / "housing.su2"
    path.write_text("NDIME= 3\nNELEM= 0\nNPOIN= 0\nNMARK= 0\n")
    return path


def test_run_thermal_case_reports_the_sensor_bias(thermal_mesh, tmp_path):
    """A completed run yields the die temperature and the measurement error."""
    runner = FakeRunner(lines=["SU2 multizone", "Outer iteration 1", "done"])
    result = run_thermal_case(
        make_params(),
        mesh_path=thermal_mesh,
        working_directory=tmp_path / "run",
        runner=runner,
        solver=SolverParams(mpi_ranks=1),
        sim_id="therm-1",
    )

    assert result.sim_id == "therm-1"
    assert result.ambient_temp_c == pytest.approx(25.0)
    assert result.delta_t_error_k == pytest.approx(
        result.sensor_temp_k - 298.15, abs=1e-9
    )
    assert result.sensor_temp_c == pytest.approx(
        kelvin_to_celsius(result.sensor_temp_k)
    )
    assert result.roof_temp_k > result.sensor_temp_k
    assert result.tube_mass_flow_kg_s > 0.0


def test_run_thermal_case_writes_a_config(thermal_mesh, tmp_path):
    """The conjugate configuration lands in the run directory."""
    work = tmp_path / "run"
    runner = FakeRunner(lines=["ok"])
    run_thermal_case(
        make_params(),
        mesh_path=thermal_mesh,
        working_directory=work,
        runner=runner,
        solver=SolverParams(mpi_ranks=1),
    )
    assert (work / "thermal.cfg").is_file()
    assert (work / thermal_mesh.name).is_file()
    assert runner.calls[0]["config_path"] == str(work / "thermal.cfg")


def test_run_thermal_case_rejects_a_missing_mesh(tmp_path):
    """A bad mesh path is caught before the solver starts."""
    with pytest.raises(ThermalSolverError, match="mesh file not found"):
        run_thermal_case(
            make_params(),
            mesh_path=tmp_path / "absent.su2",
            working_directory=tmp_path / "run",
            runner=FakeRunner(lines=[]),
        )


def test_run_thermal_case_surfaces_solver_errors(thermal_mesh, tmp_path):
    """An SU2 error is reported rather than silently returning the estimate."""
    runner = FakeRunner(lines=["Error in CConfig: bad CHT interface"])
    with pytest.raises(ThermalSolverError, match="SU2 reported an error"):
        run_thermal_case(
            make_params(),
            mesh_path=thermal_mesh,
            working_directory=tmp_path / "run",
            runner=runner,
            solver=SolverParams(mpi_ranks=1),
        )
