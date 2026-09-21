"""Model Context Protocol server exposing the whole platform to an AI agent.

Every capability the GUI offers is reachable here, because both surfaces are
thin clients over the same typed parameter core in :mod:`core.models`. That is
what keeps the two in step: a field added to a model appears in the GUI form
and in this server's tool schema at once, with its bounds, units and
description intact.

Tools are deliberately coarse -- one per workflow step rather than one per
setting -- so an agent can go from a STEP file to a rendered result in four
calls, while still being able to override any individual parameter through the
structured arguments.
"""

from __future__ import annotations

import json
import os
import traceback
from pathlib import Path
from typing import Any, Sequence

from mcp.server.mcpserver import MCPServer

from backend.aero_solver import run_aero_case
from backend.mesh_pipeline import generate_mesh
from backend.runner import SolverRunner, SU2Runner
from backend.sweep import run_parametric_sweep
from backend.thermal_solver import (
    LumpedGeometry,
    estimate_sensor_bias,
    run_thermal_case,
)
from core.models import (
    AeroRunRequest,
    AxisDirection,
    DomainParams,
    DomainShape,
    FlowParams,
    GeometryParams,
    HingeAxis,
    MeshParams,
    MeshRequest,
    MeshResolution,
    ReferenceValues,
    SimulationTrack,
    SolverParams,
    ThermalParams,
    VelocityType,
)
from core.platform_env import probe_environment
from core.store import RecordNotFoundError, RunStore, default_store

SERVER_NAME = "aerothermalstudio"

server = MCPServer(
    SERVER_NAME,
    instructions=(
        "Rocket aerodynamics and sensor-microclimate CFD. Typical workflow: "
        "set_geometry_and_mesh to prepare a mesh, then "
        "run_aerodynamic_simulation or run_sensor_thermal_simulation, then "
        "generate_cfd_visualization to render the result. Use "
        "run_parametric_sweep for drag polars and Mach curves. All lengths "
        "are metres, angles degrees, temperatures Celsius unless a field name "
        "says otherwise."
    ),
)


def _store() -> RunStore:
    """The shared run registry, also used by the GUI."""
    return default_store()


def _runner() -> SolverRunner:
    """Solver backend, overridable for testing via a module attribute."""
    return _RUNNER_OVERRIDE if _RUNNER_OVERRIDE is not None else SU2Runner()


# Tests inject a FakeRunner here so tools can be exercised without SU2.
_RUNNER_OVERRIDE: SolverRunner | None = None


def set_runner(runner: SolverRunner | None) -> None:
    """Install a solver backend, or restore the default when given None."""
    global _RUNNER_OVERRIDE
    _RUNNER_OVERRIDE = runner


def _error(message: str, **extra: Any) -> dict[str, Any]:
    """A structured failure an agent can act on rather than a raw traceback."""
    payload: dict[str, Any] = {"ok": False, "error": message}
    payload.update(extra)
    return payload


def _ok(**payload: Any) -> dict[str, Any]:
    """A structured success response."""
    return {"ok": True, **payload}


# ---------------------------------------------------------------------------
# Tool 1: geometry and meshing
# ---------------------------------------------------------------------------


@server.tool(
    name="set_geometry_and_mesh",
    description=(
        "Import a STEP/STP CAD file, align it to the wind-tunnel frame, build "
        "the farfield domain and generate an optimised hybrid mesh with prism "
        "boundary layers. The characteristic cell size is bisected until the "
        "cell count lands inside the band for the chosen track and resolution "
        "(250k-750k for aerodynamics, 150k-400k for thermal), which keeps a "
        "solve inside the 3-8 minute target. Returns a mesh_id to pass to the "
        "simulation tools, plus the cell count, boundary marker report and "
        "achieved y+."
    ),
)
def set_geometry_and_mesh(
    step_file_path: str,
    nose_vector: list[float] | None = None,
    nose_direction: str = "+X",
    reference_origin: list[float] | None = None,
    domain_multipliers: dict[str, float] | None = None,
    domain_shape: str = "cylinder",
    mesh_resolution: str = "medium",
    track: str = "aerodynamic",
    boundary_layers: int = 7,
    target_yplus: float = 45.0,
    scale_to_meters: float = 1.0,
    sizing_mach: float = 1.0,
    sizing_altitude_m: float = 0.0,
    max_targeting_iterations: int = 4,
) -> dict[str, Any]:
    """Prepare a solver-ready mesh from a CAD file.

    Parameters
    ----------
    step_file_path:
        Absolute path to the .step/.stp file.
    nose_vector:
        Arbitrary forward direction [nx, ny, nz] in CAD coordinates. Takes
        precedence over nose_direction when given.
    nose_direction:
        Named forward axis: '+X', '-X', '+Y', '-Y', '+Z' or '-Z'.
    reference_origin:
        Point [x0, y0, z0] moved to the tunnel origin, typically the nose tip.
    domain_multipliers:
        Farfield envelope as multiples of body length, e.g.
        {"upstream": 5, "downstream": 10, "radial": 5}.
    domain_shape:
        'cylinder' for rockets or 'box' for enclosures.
    mesh_resolution:
        'coarse', 'medium' or 'fine'; selects the target cell band.
    track:
        'aerodynamic' or 'thermal'; selects the cell band and marker set.
    boundary_layers:
        Prism layers on wall surfaces, 5-8.
    target_yplus:
        y+ at the first cell centroid; 30-60 keeps wall functions valid.
    scale_to_meters:
        Multiplier converting CAD units to metres (0.001 for millimetres).
    sizing_mach, sizing_altitude_m:
        Flight condition the boundary layer is sized for.
    max_targeting_iterations:
        Remesh attempts allowed to reach the target cell band.
    """
    try:
        multipliers = domain_multipliers or {}
        geometry = GeometryParams(
            step_file_path=step_file_path,
            nose_direction=AxisDirection(nose_direction) if nose_vector is None else None,
            nose_vector=nose_vector,
            reference_origin=reference_origin or [0.0, 0.0, 0.0],
            scale_to_meters=scale_to_meters,
        )
        domain = DomainParams(
            shape=DomainShape(domain_shape),
            upstream_multiplier=float(multipliers.get("upstream", 5.0)),
            downstream_multiplier=float(multipliers.get("downstream", 10.0)),
            radial_multiplier=float(multipliers.get("radial", 5.0)),
        )
        mesh = MeshParams(
            resolution=MeshResolution(mesh_resolution),
            track=SimulationTrack(track),
            boundary_layers=boundary_layers,
            target_yplus=target_yplus,
            max_targeting_iterations=max_targeting_iterations,
        )
        request = MeshRequest(
            geometry=geometry,
            domain=domain,
            mesh=mesh,
            sizing_flow=FlowParams(
                velocity_type=VelocityType.MACH,
                velocity_value=sizing_mach,
                altitude_m=sizing_altitude_m,
            ),
        )
    except Exception as error:
        return _error(f"invalid parameters: {error}")

    store = _store()
    record = store.create("mesh", {"step_file_path": step_file_path})

    try:
        result = generate_mesh(request, record.path("mesh.su2"))
    except Exception as error:
        store.update_metadata(record.record_id, {"failed": str(error)})
        return _error(str(error), mesh_id=record.record_id)

    store.update_metadata(
        record.record_id,
        {
            "mesh_path": str(record.path("mesh.su2")),
            "cell_count": result.cell_count,
            "reference_area_m2": result.reference_area_m2,
            "reference_length_m": result.reference_length_m,
            "reference_diameter_m": result.reference_diameter_m,
            "track": track,
        },
    )
    store.write_json(record.record_id, "mesh_request.json", request)
    store.write_json(record.record_id, "mesh_result.json", result)

    return _ok(
        mesh_id=record.record_id,
        mesh_path=str(record.path("mesh.su2")),
        cell_count=result.cell_count,
        node_count=result.node_count,
        target_band=list(result.target_band),
        within_target_band=result.within_target_band,
        reference_length_m=result.reference_length_m,
        reference_diameter_m=result.reference_diameter_m,
        reference_area_m2=result.reference_area_m2,
        first_cell_height_m=result.first_cell_height_m,
        estimated_yplus=result.estimated_yplus,
        boundary_markers=result.boundary_markers,
        min_quality=result.min_quality,
        targeting_iterations=result.targeting_iterations,
        wall_time_s=result.wall_time_s,
    )


# ---------------------------------------------------------------------------
# Tool 2: aerodynamic simulation
# ---------------------------------------------------------------------------


@server.tool(
    name="run_aerodynamic_simulation",
    description=(
        "Run a RANS aerodynamic simulation on a previously generated mesh. "
        "The convective scheme is selected from the Mach number (JST central "
        "below 0.8, Roe upwind with MUSCL and the Venkatakrishnan limiter "
        "above), with the SST k-omega turbulence model. Returns dimensional "
        "force components, drag/lift/side coefficients, the centre of "
        "pressure, and the scalar servo torque about each fin hinge axis."
    ),
)
def run_aerodynamic_simulation(
    mesh_id: str,
    velocity_type: str = "mach",
    velocity_val: float = 2.0,
    aoa_deg: float = 0.0,
    sideslip_deg: float = 0.0,
    altitude_m: float = 0.0,
    hinge_axes: list[dict[str, Any]] | None = None,
    reference_area_m2: float | None = None,
    reference_length_m: float | None = None,
    moment_origin: list[float] | None = None,
    mpi_ranks: int = 10,
    max_iterations: int = 5000,
    convergence_residual: float = -5.0,
    turbulence_model: str = "SST",
    cfl_number: float = 5.0,
) -> dict[str, Any]:
    """Execute one aerodynamic simulation point.

    Parameters
    ----------
    mesh_id:
        Identifier returned by set_geometry_and_mesh.
    velocity_type:
        'mach' interprets velocity_val as a Mach number, 'tas' as m/s.
    velocity_val:
        Mach number (0.05-3.5) or true airspeed in m/s.
    aoa_deg, sideslip_deg:
        Angle of attack and sideslip in degrees, each within +/-20.
    altitude_m:
        Geopotential altitude for the standard atmosphere.
    hinge_axes:
        Fin hinge definitions, each
        {"name": str, "point": [x,y,z], "direction": [u,v,w]}. The reported
        torque is the aerodynamic moment about the point, projected onto the
        direction.
    reference_area_m2, reference_length_m, moment_origin:
        Override the values measured from the CAD.
    mpi_ranks:
        MPI ranks for the solver; 10 suits a 6-core/12-thread machine.
    max_iterations, convergence_residual, turbulence_model, cfl_number:
        Solver controls.
    """
    store = _store()
    try:
        mesh_path = store.resolve_mesh_path(mesh_id)
        mesh_record = store.get(mesh_id)
    except RecordNotFoundError as error:
        return _error(str(error), mesh_id=mesh_id)

    try:
        axes = [
            HingeAxis(
                name=axis.get("name", f"hinge_{index + 1}"),
                point=axis["point"],
                direction=axis["direction"],
            )
            for index, axis in enumerate(hinge_axes or [])
        ]
        request = AeroRunRequest(
            mesh_id=mesh_id,
            flow=FlowParams(
                velocity_type=VelocityType(velocity_type),
                velocity_value=velocity_val,
                aoa_deg=aoa_deg,
                sideslip_deg=sideslip_deg,
                altitude_m=altitude_m,
            ),
            solver=SolverParams(
                mpi_ranks=mpi_ranks,
                max_iterations=max_iterations,
                convergence_residual=convergence_residual,
                turbulence_model=turbulence_model,
                cfl_number=cfl_number,
            ),
            reference=ReferenceValues(
                reference_area_m2=reference_area_m2,
                reference_length_m=reference_length_m,
                moment_origin=moment_origin,
            ),
            hinge_axes=axes,
        )
    except Exception as error:
        return _error(f"invalid parameters: {error}")

    record = store.create("aero", {"mesh_id": mesh_id})
    try:
        result = run_aero_case(
            request,
            mesh_path=mesh_path,
            working_directory=record.directory,
            runner=_runner(),
            reference_area_m2=float(
                mesh_record.metadata.get("reference_area_m2", 1.0)
            ),
            reference_length_m=float(
                mesh_record.metadata.get("reference_length_m", 1.0)
            ),
            sim_id=record.record_id,
        )
    except Exception as error:
        store.update_metadata(record.record_id, {"failed": str(error)})
        return _error(str(error), sim_id=record.record_id)

    store.write_json(record.record_id, "result.json", result)
    store.update_metadata(
        record.record_id, {"cd": result.cd, "cl": result.cl, "mach": result.mach}
    )

    return _ok(
        sim_id=result.sim_id,
        mesh_id=mesh_id,
        mach=result.mach,
        speed_ms=result.speed_ms,
        aoa_deg=result.aoa_deg,
        sideslip_deg=result.sideslip_deg,
        dynamic_pressure_pa=result.dynamic_pressure_pa,
        forces_n={
            "fx": result.force_x_n,
            "fy": result.force_y_n,
            "fz": result.force_z_n,
        },
        drag_n=result.drag_n,
        lift_n=result.lift_n,
        sideforce_n=result.sideforce_n,
        coefficients={
            "cd": result.cd,
            "cl": result.cl,
            "cs": result.cs,
            "cm_pitch": result.cm_pitch,
        },
        center_of_pressure=result.center_of_pressure,
        hinge_torques=[
            {
                "name": torque.name,
                "torque_nm": torque.torque_nm,
                "moment_vector_nm": torque.moment_vector_nm,
            }
            for torque in result.hinge_torques
        ],
        iterations=result.iterations,
        final_residual_rho=result.final_residual_rho,
        converged=result.converged,
        wall_time_s=result.wall_time_s,
    )


# ---------------------------------------------------------------------------
# Tool 3: sensor thermal simulation
# ---------------------------------------------------------------------------


@server.tool(
    name="run_sensor_thermal_simulation",
    description=(
        "Predict the temperature a BMP580 sensor reads inside a 3D-printed "
        "enclosure with a ram-air sampling tube, mounted above a "
        "solar-irradiated surface such as a tram roof. Solves the coupled "
        "roof, housing, tube-flow and chamber energy balances, and runs the "
        "SU2 multizone conjugate solve when a thermal mesh_id is supplied. "
        "Returns the die temperature and the measurement bias relative to "
        "true ambient. Set analytic_only for an instant estimate with no mesh."
    ),
)
def run_sensor_thermal_simulation(
    enclosure_step: str = "",
    mesh_id: str | None = None,
    vehicle_speed_ms: float = 2.0,
    ambient_temp_c: float = 25.0,
    solar_flux_w_m2: float = 800.0,
    height_above_roof_m: float = 0.1,
    sensor_xyz: list[float] | None = None,
    housing_conductivity_w_mk: float = 0.18,
    housing_solar_absorptivity: float = 0.30,
    housing_emissivity: float = 0.90,
    roof_solar_absorptivity: float = 0.65,
    roof_emissivity: float = 0.85,
    sensor_power_w: float = 1.0e-3,
    analytic_only: bool = False,
    mpi_ranks: int = 10,
) -> dict[str, Any]:
    """Predict the BMP580 reading and its bias.

    Parameters
    ----------
    enclosure_step:
        Path to the enclosure CAD, recorded with the run.
    mesh_id:
        Thermal mesh to solve on. Omit, or set analytic_only, to use the
        analytical model alone.
    vehicle_speed_ms:
        Vehicle speed driving ram air through the sampling tube.
    ambient_temp_c:
        True ambient air temperature in Celsius.
    solar_flux_w_m2:
        Global solar irradiance on the roof.
    height_above_roof_m:
        Intake height above the irradiated surface. Compared against the
        roof's thermal boundary-layer thickness to decide whether the intake
        samples ambient air.
    sensor_xyz:
        BMP580 die coordinate [x, y, z] in metres, in the enclosure frame.
    housing_* , roof_*:
        Material optical and thermal properties.
    sensor_power_w:
        Self-heating dissipation, typically 1 mW.
    analytic_only:
        Skip the CFD solve and return the analytical prediction.
    """
    try:
        params = ThermalParams(
            enclosure_step_path=enclosure_step or "(not supplied)",
            vehicle_speed_ms=vehicle_speed_ms,
            ambient_temp_c=ambient_temp_c,
            solar_flux_w_m2=solar_flux_w_m2,
            height_above_roof_m=height_above_roof_m,
            sensor_xyz=sensor_xyz or [0.03, 0.02, 0.015],
            housing_conductivity_w_mk=housing_conductivity_w_mk,
            housing_solar_absorptivity=housing_solar_absorptivity,
            housing_emissivity=housing_emissivity,
            roof_solar_absorptivity=roof_solar_absorptivity,
            roof_emissivity=roof_emissivity,
            sensor_power_w=sensor_power_w,
        )
    except Exception as error:
        return _error(f"invalid parameters: {error}")

    analytic = estimate_sensor_bias(params)

    if analytic_only or mesh_id is None:
        return _ok(
            method="analytic",
            sensor_temp_c=analytic.sensor_temp_k - 273.15,
            sensor_temp_k=analytic.sensor_temp_k,
            ambient_temp_c=ambient_temp_c,
            delta_t_error_k=analytic.delta_t_error_k,
            roof_temp_c=analytic.roof_temp_k - 273.15,
            housing_temp_c=analytic.housing_temp_k - 273.15,
            tube_mass_flow_kg_s=analytic.tube_mass_flow_kg_s,
            tube_velocity_ms=analytic.tube_velocity_ms,
            roof_thermal_boundary_layer_m=analytic.thermal_boundary_layer_m,
            intake_in_roof_plume=analytic.intake_in_roof_plume,
            self_heating_rise_k=analytic.self_heating_rise_k,
            wall_coupling_rise_k=analytic.wall_coupling_rise_k,
        )

    store = _store()
    try:
        mesh_path = store.resolve_mesh_path(mesh_id)
    except RecordNotFoundError as error:
        return _error(str(error), mesh_id=mesh_id)

    record = store.create("thermal", {"mesh_id": mesh_id})
    try:
        result = run_thermal_case(
            params,
            mesh_path=mesh_path,
            working_directory=record.directory,
            runner=_runner(),
            solver=SolverParams(mpi_ranks=mpi_ranks),
            sim_id=record.record_id,
        )
    except Exception as error:
        store.update_metadata(record.record_id, {"failed": str(error)})
        return _error(str(error), sim_id=record.record_id)

    store.write_json(record.record_id, "result.json", result)
    return _ok(
        method="conjugate_cfd",
        sim_id=result.sim_id,
        mesh_id=mesh_id,
        sensor_temp_c=result.sensor_temp_c,
        sensor_temp_k=result.sensor_temp_k,
        ambient_temp_c=result.ambient_temp_c,
        delta_t_error_k=result.delta_t_error_k,
        roof_temp_c=result.roof_temp_k - 273.15,
        housing_max_temp_c=result.housing_max_temp_k - 273.15,
        chamber_mean_temp_c=result.chamber_mean_temp_k - 273.15,
        tube_mass_flow_kg_s=result.tube_mass_flow_kg_s,
        analytic_delta_t_error_k=analytic.delta_t_error_k,
        iterations=result.iterations,
        converged=result.converged,
        wall_time_s=result.wall_time_s,
    )


# ---------------------------------------------------------------------------
# Tool 4: visualisation
# ---------------------------------------------------------------------------


@server.tool(
    name="generate_cfd_visualization",
    description=(
        "Render a publication-quality image from a completed simulation. "
        "Modes: 'surface_pressure' (body Cp or absolute pressure), "
        "'mach_slice' (cutting plane showing oblique shocks and "
        "Prandtl-Meyer expansion), 'streamlines' (seeded flow paths coloured "
        "by velocity or temperature) and 'thermal' (temperature contours with "
        "the sensor marked). Saves a PNG and returns its path."
    ),
)
def generate_cfd_visualization(
    sim_id: str,
    visualization_type: str = "surface_pressure",
    camera_view: str = "isometric",
    slice_normal: list[float] | None = None,
    colormap: str = "turbo",
    resolution: str = "4k",
    output_path: str | None = None,
) -> dict[str, Any]:
    """Render a result image.

    Parameters
    ----------
    sim_id:
        Identifier of a completed simulation.
    visualization_type:
        'surface_pressure', 'mach_slice', 'streamlines' or 'thermal'.
    camera_view:
        'isometric', 'front', 'back', 'side', 'top', 'bottom',
        'nose_quarter' or 'tail_quarter'.
    slice_normal:
        Cutting-plane normal for slice modes; defaults to the symmetry plane.
    colormap:
        'turbo', 'coolwarm', 'viridis', 'jet', 'plasma' or 'inferno'.
    resolution:
        'preview', 'hd', '2k' or '4k'.
    output_path:
        Destination PNG; defaults to a file inside the run directory.
    """
    store = _store()
    try:
        record = store.get(sim_id)
    except RecordNotFoundError as error:
        return _error(str(error), sim_id=sim_id)

    try:
        from backend.visualizer import render_visualization
    except Exception as error:  # pragma: no cover - optional dependency
        return _error(f"visualisation unavailable: {error}")

    destination = Path(
        output_path
        or record.path("renders", f"{visualization_type}_{camera_view}.png")
    )

    try:
        path = render_visualization(
            record.directory,
            visualization_type,
            destination,
            camera_view=camera_view,
            slice_normal=tuple(slice_normal or (0.0, 1.0, 0.0)),
            colormap=colormap,
            resolution=resolution,
        )
    except Exception as error:
        return _error(str(error), sim_id=sim_id)

    return _ok(
        sim_id=sim_id,
        image_path=str(path),
        visualization_type=visualization_type,
        camera_view=camera_view,
        colormap=colormap,
        resolution=resolution,
    )


# ---------------------------------------------------------------------------
# Tool 5: parametric sweep
# ---------------------------------------------------------------------------


@server.tool(
    name="run_parametric_sweep",
    description=(
        "Run a batch of aerodynamic simulations across a swept parameter "
        "('mach', 'aoa', 'sideslip' or 'altitude'), reusing one mesh. Cases "
        "run sequentially so memory stays within a 16 GB budget, and a point "
        "that fails is recorded without discarding the rest. Returns summary "
        "curves plus the peak values a designer sizes hardware from, "
        "including the maximum hinge torque across the sweep."
    ),
)
def run_parametric_sweep_tool(
    mesh_id: str,
    param_name: str = "mach",
    values: list[float] | None = None,
    fixed_params: dict[str, Any] | None = None,
    hinge_axes: list[dict[str, Any]] | None = None,
    mpi_ranks: int = 10,
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """Sweep one parameter across a list of values.

    Parameters
    ----------
    mesh_id:
        Mesh shared by every point.
    param_name:
        Parameter to vary: 'mach', 'aoa', 'sideslip' or 'altitude'.
    values:
        Values to run, e.g. [0.5, 1.0, 1.5, 2.0, 2.5].
    fixed_params:
        Conditions held constant, e.g.
        {"aoa_deg": 5, "altitude_m": 1000, "velocity_type": "mach",
         "velocity_val": 2.0}.
    hinge_axes:
        Fin hinge definitions, as for run_aerodynamic_simulation.
    mpi_ranks:
        MPI ranks per case.
    stop_on_error:
        Abort at the first failure instead of continuing.
    """
    store = _store()
    try:
        mesh_path = store.resolve_mesh_path(mesh_id)
        mesh_record = store.get(mesh_id)
    except RecordNotFoundError as error:
        return _error(str(error), mesh_id=mesh_id)

    if not values:
        return _error("a sweep needs at least one value in 'values'")

    fixed = dict(fixed_params or {})
    try:
        axes = [
            HingeAxis(
                name=axis.get("name", f"hinge_{index + 1}"),
                point=axis["point"],
                direction=axis["direction"],
            )
            for index, axis in enumerate(hinge_axes or [])
        ]
        base = AeroRunRequest(
            mesh_id=mesh_id,
            flow=FlowParams(
                velocity_type=VelocityType(fixed.get("velocity_type", "mach")),
                velocity_value=float(fixed.get("velocity_val", 2.0)),
                aoa_deg=float(fixed.get("aoa_deg", 0.0)),
                sideslip_deg=float(fixed.get("sideslip_deg", 0.0)),
                altitude_m=float(fixed.get("altitude_m", 0.0)),
            ),
            solver=SolverParams(mpi_ranks=mpi_ranks),
            hinge_axes=axes,
        )
    except Exception as error:
        return _error(f"invalid parameters: {error}")

    record = store.create("sweep", {"mesh_id": mesh_id, "parameter": param_name})
    try:
        sweep = run_parametric_sweep(
            base,
            parameter=param_name,
            values=values,
            mesh_path=mesh_path,
            output_root=record.directory,
            runner=_runner(),
            reference_area_m2=float(
                mesh_record.metadata.get("reference_area_m2", 1.0)
            ),
            reference_length_m=float(
                mesh_record.metadata.get("reference_length_m", 1.0)
            ),
            stop_on_error=stop_on_error,
        )
    except Exception as error:
        store.update_metadata(record.record_id, {"failed": str(error)})
        return _error(str(error), sweep_id=record.record_id)

    store.write_json(record.record_id, "sweep.json", sweep.as_dict())
    return _ok(sweep_id=record.record_id, **sweep.as_dict())


# ---------------------------------------------------------------------------
# Supporting tools
# ---------------------------------------------------------------------------


@server.tool(
    name="list_runs",
    description=(
        "List stored meshes and simulations, newest first. Use this to "
        "rediscover a mesh_id or sim_id from an earlier session; the GUI and "
        "this server share one registry."
    ),
)
def list_runs(kind: str | None = None, limit: int = 25) -> dict[str, Any]:
    """List stored records.

    Parameters
    ----------
    kind:
        Filter by 'mesh', 'aero', 'thermal' or 'sweep'.
    limit:
        Maximum records to return.
    """
    store = _store()
    try:
        records = store.list_records(kind)  # type: ignore[arg-type]
    except Exception as error:
        return _error(str(error))
    return _ok(
        records=[
            {
                "record_id": record.record_id,
                "kind": record.kind,
                "created_at": record.created_at,
                "metadata": record.metadata,
            }
            for record in records[: max(1, limit)]
        ]
    )


@server.tool(
    name="check_environment",
    description=(
        "Report whether the native solver toolchain is installed and ready: "
        "Microsoft MPI, the SU2 binaries, CPU count and the data directory. "
        "Call this first when a simulation tool reports that SU2 is missing."
    ),
)
def check_environment() -> dict[str, Any]:
    """Report the detected toolchain."""
    return _ok(**probe_environment(probe_versions=True).as_dict())


def main() -> None:
    """Run the MCP server on stdio."""
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - entry point
    main()
