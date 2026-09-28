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
import math
import re
import os
import time
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
from core.frames import AXIS_CONVENTION, Frame
from core.models import (
    AeroRunRequest,
    AxisDirection,
    BodyKind,
    ConvectiveScheme,
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
from core.platform_env import data_root, probe_environment
from core.project import (
    PROJECT_EXTENSION,
    Project,
    ProjectError,
    SweepSettings,
    default_project,
)
from core.project import list_projects as _list_project_files
from core.project import projects_directory
from core.settings import AppSettings, load_settings, save_settings
from core.step_inspect import (
    StepInspectionError,
    detect_body_axis,
    suggest_scale_to_meters,
)
from core.store import RUN_ID_PATTERN, RecordNotFoundError, RunStore, default_store
from core.workspace import active_geometry, set_active_geometry

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


# Whoever wants to watch a solve the assistant started -- the in-app chat's
# live convergence chart. Called on the solver's thread; listeners marshal
# to their own. A listener that raises is dropped rather than allowed to
# kill the solve it was only watching.
_solver_listeners: list[Any] = []


def add_solver_listener(listener: Any) -> None:
    """Receive ``("iteration", record)`` and ``("line", text)`` events."""
    if listener not in _solver_listeners:
        _solver_listeners.append(listener)


def remove_solver_listener(listener: Any) -> None:
    """Stop receiving solver events."""
    if listener in _solver_listeners:
        _solver_listeners.remove(listener)


def _broadcast(kind: str, payload: Any) -> None:
    for listener in list(_solver_listeners):
        try:
            listener(kind, payload)
        except Exception:  # noqa: BLE001 - a watcher must not stop a solve
            remove_solver_listener(listener)


def _broadcast_iteration(record: Any) -> None:
    _broadcast("iteration", record)


def _broadcast_line(line: str) -> None:
    _broadcast("line", line)


def _broadcast_finished() -> None:
    """The solve is over: the live chart has nothing more to show."""
    _broadcast("finished", None)


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
# Showing the operator the setup before paying for it
# ---------------------------------------------------------------------------

# The in-app assistant turns this on. A setup -- which way the air comes
# from, what the model tilts about, at what angle -- must have been drawn by
# preview_orientation and the operator must have replied since, before it is
# meshed or solved. External MCP clients leave it off: they have their own
# conversation with the operator and no chat panel to draw into.
_confirmation_required = False
# Setups drawn but not yet answered, and setups the operator has seen.
_shown_setups: set[str] = set()
_confirmed_setups: set[str] = set()


def require_orientation_confirmation(required: bool) -> None:
    """Make meshing and solving wait for an approved preview (or not)."""
    global _confirmation_required
    _confirmation_required = bool(required)


def acknowledge_orientation_previews() -> None:
    """The operator has replied: what they were shown is theirs to have approved.

    Called when the operator sends a message. If the reply was "no, the air
    comes from the other end", the assistant changes the setup, and a
    changed setup is a new key that has not been shown -- so it must draw it
    again before it can mesh.
    """
    _confirmed_setups.update(_shown_setups)
    _shown_setups.clear()


def reset_orientation_confirmations() -> None:
    """Forget every approval, as a new conversation should."""
    _shown_setups.clear()
    _confirmed_setups.clear()


def _geometry_key(geometry: GeometryParams) -> list[Any]:
    """What about a geometry decides how the air meets it."""
    pitch = _pitch_or_none(geometry)
    return [
        str(Path(geometry.step_file_path).expanduser().resolve()),
        [round(float(v), 4) for v in geometry.resolved_nose_vector()],
        pitch,
        geometry.body_kind.value,
    ]


def _pitch_or_none(geometry: GeometryParams) -> list[float] | None:
    if geometry.pitch_axis is None:
        return None
    return [round(float(v), 4) for v in geometry.pitch_axis.to_vector()]


def _setup_key(geometry_key: Any, aoa_deg: float | None, sideslip_deg: float | None) -> str:
    angles = (
        None
        if aoa_deg is None
        else [round(float(aoa_deg), 3), round(float(sideslip_deg or 0.0), 3)]
    )
    return json.dumps([geometry_key, angles])


def _setup_confirmed(geometry_key: Any, aoa_deg: float | None, sideslip_deg: float | None) -> bool:
    if aoa_deg is not None:
        return _setup_key(geometry_key, aoa_deg, sideslip_deg) in _confirmed_setups
    # For meshing only the geometry matters: any approved angle will do.
    prefix = json.dumps([geometry_key, None])[:-5]
    return any(key.startswith(prefix) for key in _confirmed_setups)


def _orientation_gate(
    geometry_key: Any,
    aoa_deg: float | None = None,
    sideslip_deg: float | None = None,
    **where: Any,
) -> dict[str, Any] | None:
    """A refusal when this setup has not been shown and approved, else None."""
    if not _confirmation_required:
        return None
    if _setup_confirmed(geometry_key, aoa_deg, sideslip_deg):
        return None
    angles = (
        ""
        if aoa_deg is None
        else f" with aoa_deg={aoa_deg:g}, sideslip_deg={float(sideslip_deg or 0.0):g}"
    )
    return _error(
        "the operator has not approved this setup yet. Call preview_orientation"
        f"{angles} with the same geometry settings, tell the operator what the "
        "picture shows (where the air comes from, what the model tilts "
        "about), ask whether that is what they want, and END YOUR TURN. Mesh "
        "or solve only after they reply; if they correct it, preview the "
        "corrected setup again.",
        needs=["operator_confirmation"],
        **where,
    )


def _mesh_geometry_key(store: Any, mesh_record: Any) -> Any:
    """The geometry key a mesh was built with, or None if unknown."""
    key = mesh_record.metadata.get("geometry_key")
    if key is not None:
        return key
    try:
        request = MeshRequest.model_validate(
            store.read_json(mesh_record.record_id, "mesh_request.json")
        )
        return _geometry_key(request.geometry)
    except Exception:  # noqa: BLE001 - an old record: fall back to the angles
        return None


# ---------------------------------------------------------------------------
# Tool 1: geometry and meshing
# ---------------------------------------------------------------------------


def _resolve_geometry(
    step_file_path: str,
    nose_vector: list[float] | None,
    nose_direction: str | None,
    reference_origin: list[float] | None,
    scale_to_meters: float | None,
    body_kind: str | None,
    pitch_axis: str | None,
) -> dict[str, Any]:
    """Work out file, scale, nose, body kind and tilt axis as meshing will.

    Shared by the mesher and the orientation preview, so the picture the
    operator approves is exactly the setup that is then meshed. Returns
    ``{"error": reply}`` when something has to be asked first.
    """
    loaded = active_geometry()
    scale_note = ""
    nose_note = ""
    # No default: a deliberate "+X" used to be indistinguishable from an
    # unset one, so a fin whose air arrives at its -X edge could not be set.
    nose_given = nose_direction is not None or nose_vector is not None

    if not step_file_path:
        if loaded is None:
            return {"error": _error(
                "no STEP file given and none is loaded. Pass step_file_path, "
                "or open a file in the application's Rocket Aerodynamics tab "
                "and call get_active_geometry to confirm it."
            )}
        step_file_path = loaded.step_file_path
        if scale_to_meters is None:
            scale_to_meters = loaded.scale_to_meters
            scale_note = loaded.scale_reason

    if scale_to_meters is None:
        # Reading the file's declared unit beats assuming metres: a
        # millimetre model taken at face value is a kilometre-long rocket,
        # and nothing downstream would flag it.
        try:
            decision = suggest_scale_to_meters(step_file_path)
            scale_to_meters = decision.scale
            scale_note = decision.reason
        except StepInspectionError as error:
            return {"error": _error(f"could not read '{step_file_path}': {error}")}

    if (
        not nose_given
        and loaded is not None
        and loaded.step_file_path == step_file_path
        and loaded.nose_is_confident
    ):
        # The operator may have set it in the interface; that beats reading
        # it off the shape again.
        nose_direction = loaded.nose_direction
        nose_note = loaded.nose_reason
        nose_given = True

    same_file = loaded is not None and loaded.step_file_path == step_file_path
    if body_kind is None:
        if same_file:
            body_kind = loaded.body_kind
            pitch_axis = pitch_axis or loaded.pitch_axis
        else:
            try:
                shape = detect_body_axis(step_file_path)
            except StepInspectionError:
                shape = None
            body_kind = shape.kind if shape is not None else "rocket"
            if pitch_axis is None and shape is not None and shape.tilt_axis:
                pitch_axis = f"+{shape.tilt_axis}"

    if not nose_given:
        # Which way the body points is not a default worth having. A rocket
        # meshed backwards returns a full set of plausible forces for a
        # vehicle flying tail-first, and nothing downstream objects.
        try:
            axis = detect_body_axis(step_file_path)
        except StepInspectionError as error:
            return {"error": _error(f"could not read '{step_file_path}': {error}")}
        if axis.nose_direction is None and body_kind == "fin":
            return {"error": _error(
                f"this is a fin; which edge faces the oncoming air? {axis.reason}. "
                f"Ask the operator, then pass nose_direction: '+{axis.axis}' if "
                f"the air arrives at the -{axis.axis} edge, or '-{axis.axis}' "
                f"if it arrives at the +{axis.axis} edge.",
                axis=axis.axis,
                needs=["nose_direction"],
                geometry=axis.as_dict(),
            )}
        if axis.nose_direction is None:
            return {"error": _error(
                f"which end is the nose? {axis.reason}. Ask the operator, "
                f"then pass nose_direction: '+{axis.axis}' if the nose is at "
                f"the -{axis.axis} end of the CAD model, or '-{axis.axis}' "
                f"if it is at the +{axis.axis} end.",
                axis=axis.axis,
                needs=["nose_direction"],
                geometry=axis.as_dict(),
            )}
        nose_direction = axis.nose_direction
        nose_note = axis.reason

    try:
        geometry = GeometryParams(
            step_file_path=step_file_path,
            nose_direction=AxisDirection(nose_direction) if nose_vector is None else None,
            nose_vector=nose_vector,
            reference_origin=reference_origin or [0.0, 0.0, 0.0],
            scale_to_meters=scale_to_meters,
            body_kind=BodyKind(body_kind),
            pitch_axis=AxisDirection(pitch_axis) if pitch_axis else None,
        )
    except Exception as error:
        return {"error": _error(f"invalid parameters: {error}")}
    return {
        "loaded": loaded,
        "geometry": geometry,
        "nose_direction": (
            geometry.nose_direction.value if geometry.nose_direction else nose_direction
        ),
        "nose_note": nose_note,
        "scale_note": scale_note,
        "body_kind": body_kind,
        "pitch_axis": geometry.pitch_axis.value if geometry.pitch_axis else None,
    }


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
    step_file_path: str = "",
    nose_vector: list[float] | None = None,
    nose_direction: str | None = None,
    reference_origin: list[float] | None = None,
    domain_multipliers: dict[str, float] | None = None,
    domain_shape: str = "cylinder",
    mesh_resolution: str = "medium",
    track: str = "aerodynamic",
    boundary_layers: int = 7,
    target_yplus: float = 45.0,
    scale_to_meters: float | None = None,
    sizing_mach: float = 1.0,
    sizing_altitude_m: float = 0.0,
    max_targeting_iterations: int = 4,
    body_kind: str | None = None,
    pitch_axis: str | None = None,
) -> dict[str, Any]:
    """Prepare a solver-ready mesh from a CAD file.

    Parameters
    ----------
    step_file_path:
        Absolute path to the .step/.stp file. Leave it out to mesh the file
        currently loaded in the desktop application; get_active_geometry
        reports what that is.
    nose_vector:
        Arbitrary forward direction [nx, ny, nz] in CAD coordinates. Takes
        precedence over nose_direction when given.
    nose_direction:
        The CAD axis the body runs along **from the nose towards the
        tail**: '+X', '-X', '+Y', '-Y', '+Z' or '-Z'. A model drawn
        nose-up along +Y takes '-Y'. Omit it and the shape decides -- the
        tapering end is the nose -- and when the shape does not say, the
        call fails asking which end it is rather than guessing.
    reference_origin:
        Point [x0, y0, z0] moved to the tunnel origin, e.g. the CG. Omit it
        and the nose tip is put at the origin, so the centre of pressure and
        hinge points read as distances from the nose.
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
        Omit it to use the unit the file declares, checked against the
        model's size; the reply says which was used and why.
    sizing_mach, sizing_altitude_m:
        Flight condition the boundary layer is sized for.
    max_targeting_iterations:
        Remesh attempts allowed to reach the target cell band.
    body_kind:
        'rocket' or 'fin'. Omit it and the shape decides: a body whose
        longest side is more than 3.5 times each of the others is a
        rocket; a thin plate-like one is a fin. For a fin, nose_direction
        is the direction the air flows along it, leading edge to trailing
        edge, and the coefficients are referenced to chord and planform.
    pitch_axis:
        CAD axis the model tilts about when the angle of attack changes,
        e.g. '+Z' for a fin whose span runs along Z. It must be across the
        flow. Omit it: a fin tilts about its span, a rocket keeps the
        orientation that follows from its nose.
    """
    resolved = _resolve_geometry(
        step_file_path, nose_vector, nose_direction, reference_origin,
        scale_to_meters, body_kind, pitch_axis,
    )
    if "error" in resolved:
        return resolved["error"]
    loaded = resolved["loaded"]
    geometry = resolved["geometry"]
    step_file_path = geometry.step_file_path
    scale_to_meters = geometry.scale_to_meters
    nose_direction = resolved["nose_direction"]
    nose_note = resolved["nose_note"]
    scale_note = resolved["scale_note"]
    body_kind = resolved["body_kind"]
    pitch_axis = resolved["pitch_axis"]

    gate = _orientation_gate(_geometry_key(geometry))
    if gate is not None:
        return gate

    try:
        multipliers = domain_multipliers or {}
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

    # Whichever surface named the file, both now agree on it: the operator
    # sees the assistant's choice in the interface, and a later call needs
    # no path.
    if loaded is None or loaded.step_file_path != step_file_path:
        set_active_geometry(
            step_file_path,
            scale_to_meters=scale_to_meters,
            nose_direction=nose_direction if nose_vector is None else None,
            source="mcp",
        )

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
            "geometry_key": _geometry_key(geometry),
            # The mesher put the nose tip at the origin (see _apply_alignment).
            "nose_at_origin": not any(geometry.reference_origin),
        },
    )
    store.write_json(record.record_id, "mesh_request.json", request)
    store.write_json(record.record_id, "mesh_result.json", result)

    return _ok(
        mesh_id=record.record_id,
        mesh_path=str(record.path("mesh.su2")),
        step_file_path=step_file_path,
        scale_to_meters=scale_to_meters,
        scale_note=scale_note,
        nose_direction=nose_direction,
        nose_note=nose_note,
        body_kind=body_kind,
        pitch_axis=pitch_axis,
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
        poor_prism_count=result.poor_prism_count,
        targeting_iterations=result.targeting_iterations,
        wall_time_s=result.wall_time_s,
    )


def _nose_end(nose_direction: str) -> str:
    """'+Y' (nose-to-tail along +Y) puts the nose at the CAD -Y end."""
    if len(nose_direction) == 2 and nose_direction[0] in "+-":
        return ("-" if nose_direction[0] == "+" else "+") + nose_direction[1]
    return "custom"


@server.tool(
    name="preview_orientation",
    description=(
        "Draw a quick low-resolution picture of the model with the oncoming "
        "air (blue arrows) and the tilt axis (orange rod and curved arrow), "
        "exactly as a mesh or solve with these settings would use them. "
        "Takes the same geometry arguments as set_geometry_and_mesh (or a "
        "mesh_id), plus aoa_deg and sideslip_deg. In the desktop assistant a "
        "new setup must be previewed and the operator must answer before it "
        "can be meshed or solved: call this, describe the picture, ask "
        "whether it is right, and end the turn."
    ),
)
def preview_orientation(
    step_file_path: str = "",
    nose_vector: list[float] | None = None,
    nose_direction: str | None = None,
    reference_origin: list[float] | None = None,
    scale_to_meters: float | None = None,
    body_kind: str | None = None,
    pitch_axis: str | None = None,
    mesh_id: str | None = None,
    aoa_deg: float = 0.0,
    sideslip_deg: float = 0.0,
    pivot_height_m: float = 0.0,
) -> dict[str, Any]:
    """Show the operator how the air will meet the model, before any solve.

    Parameters
    ----------
    step_file_path, nose_vector, nose_direction, reference_origin,
    scale_to_meters, body_kind, pitch_axis:
        As for set_geometry_and_mesh; leave them out to use the loaded file
        and what the shape says.
    mesh_id:
        Preview the geometry an existing mesh was built from instead.
    aoa_deg, sideslip_deg:
        The flow angles to draw.
    pivot_height_m:
        Moves the tilt axis (the pivot) up (+) or down (-) from the body
        axis, in metres, relative to the air at zero angle of attack: "up"
        is the side a positive angle of attack lifts the model towards
        (rocket +X). The flow is unchanged -- the solver tilts the air, not
        the model -- but pitching moments (cm_pitch) are taken about the
        pivot.
    """
    store = _store()
    if mesh_id:
        try:
            mesh_record = store.get(mesh_id)
            request = MeshRequest.model_validate(
                store.read_json(mesh_id, "mesh_request.json")
            )
        except Exception as error:  # noqa: BLE001 - reported
            return _error(f"cannot preview mesh {mesh_id}: {error}", mesh_id=mesh_id)
        geometry = request.geometry
        key = _mesh_geometry_key(store, mesh_record)
        nose_label = (
            geometry.nose_direction.value if geometry.nose_direction else "custom vector"
        )
        kind = geometry.body_kind.value
        pitch_label = geometry.pitch_axis.value if geometry.pitch_axis else None
    else:
        resolved = _resolve_geometry(
            step_file_path, nose_vector, nose_direction, reference_origin,
            scale_to_meters, body_kind, pitch_axis,
        )
        if "error" in resolved:
            return resolved["error"]
        geometry = resolved["geometry"]
        key = _geometry_key(geometry)
        nose_label = resolved["nose_direction"] if nose_vector is None else "custom vector"
        kind = resolved["body_kind"]
        pitch_label = resolved["pitch_axis"]

    from gui.flow_overlay import describe

    tilt = f"CAD {pitch_label}" if pitch_label else "the automatic axis"
    nose_end = _nose_end(nose_label)
    front = "leading edge" if kind == "fin" else "nose"
    caption = (
        f"{kind}, {front} at the {nose_end} end of the CAD model | tilt about "
        f"{tilt} | alpha {aoa_deg:g} deg, beta {sideslip_deg:g} deg"
    )
    if pivot_height_m:
        caption += f" | pivot {pivot_height_m:+g} m"

    folder = data_root() / "previews"
    stem = f"orientation-{time.strftime('%Y%m%d-%H%M%S')}"
    destination = folder / f"{stem}.png"
    counter = 2
    while destination.exists():
        destination = folder / f"{stem}-{counter}.png"
        counter += 1
    try:
        from backend.orientation_preview import render_orientation_preview

        path = render_orientation_preview(
            geometry, destination, aoa_deg, sideslip_deg, caption=caption,
            pivot_height_m=pivot_height_m,
        )
    except Exception as error:  # noqa: BLE001 - reported
        return _error(f"could not draw the preview: {error}")

    _shown_setups.add(_setup_key(key, aoa_deg, sideslip_deg))
    return _ok(
        image_path=str(path),
        body_kind=kind,
        nose_direction=nose_label,
        nose_at=f"the {nose_end} end of the CAD model",
        pitch_axis=pitch_label,
        aoa_deg=aoa_deg,
        sideslip_deg=sideslip_deg,
        pivot_height_m=pivot_height_m,
        shows=describe(aoa_deg, sideslip_deg, pitch_label, pivot_height_m)
        + " The model is drawn in rocket axes: nose up along +Z.",
        next_step=(
            "Tell the operator what the picture shows and ask whether the air "
            "direction and tilt axis are what they want. Then stop and wait "
            "for their answer."
        ),
    )


# ---------------------------------------------------------------------------
# Tool 2: aerodynamic simulation
# ---------------------------------------------------------------------------

def _iteration_limit() -> int:
    """The operator's iteration limit (AI Assistant tab, or update_settings)."""
    try:
        return load_settings(_store().root, refresh=True).solver_max_iterations
    except Exception:  # noqa: BLE001 - a broken settings file is not fatal
        return AppSettings().solver_max_iterations


def _cop_from_nose(cop_rocket: Any, mesh_record: Any) -> dict[str, Any]:
    """The centre of pressure as a distance behind the nose tip.

    Only for meshes whose nose tip sits at the origin; on older meshes the
    origin is wherever the CAD had it, and a number that looks like a
    distance from the nose would not be one.
    """
    if not mesh_record.metadata.get("nose_at_origin"):
        return {
            "center_of_pressure_note": (
                "this mesh predates nose-at-origin meshing: the centre of "
                "pressure is relative to the CAD origin, not the nose tip. "
                "Remesh to get it measured from the nose."
            )
        }
    try:
        behind = -float(cop_rocket[2])
    except (TypeError, IndexError, ValueError):
        return {}
    if not math.isfinite(behind):
        return {"center_of_pressure_behind_nose_m": None}
    length = float(mesh_record.metadata.get("reference_length_m") or 0.0)
    reply: dict[str, Any] = {"center_of_pressure_behind_nose_m": behind}
    if length > 0.0:
        reply["center_of_pressure_fraction_of_length"] = behind / length
    return reply


def _hinge_axes(hinge_axes: list[dict[str, Any]] | None) -> list[HingeAxis]:
    """Build hinge axes from tool arguments, in the rocket frame by default.

    The model default is the solver frame, because projects saved before the
    rocket frame existed are in it. Anything arriving through a tool now is
    in the frame the operator is shown, unless it says otherwise.
    """
    return [
        HingeAxis(
            name=axis.get("name", f"hinge_{index + 1}"),
            point=axis["point"],
            direction=axis["direction"],
            marker=axis.get("marker"),
            frame=Frame(axis.get("frame", Frame.ROCKET.value)),
        )
        for index, axis in enumerate(hinge_axes or [])
    ]



def _pivot_origin(
    moment_origin: list[float] | None, pivot_height_m: float | None
) -> list[float] | None:
    """An explicit moment origin wins; otherwise the pivot, if one is set."""
    if moment_origin is not None or not pivot_height_m:
        return moment_origin
    from gui.flow_overlay import pivot_moment_origin

    return pivot_moment_origin(pivot_height_m)


@server.tool(
    name="run_aerodynamic_simulation",
    description=(
        "Run a RANS aerodynamic simulation on a previously generated mesh. "
        "The solver is selected from the Mach number (incompressible below "
        "0.3, JST central to 0.8, Roe upwind with MUSCL and the "
        "Venkatakrishnan limiter above), with the SST k-omega turbulence model. Returns dimensional "
        "force components, drag/lift/side coefficients, the centre of "
        "pressure, and the scalar servo torque about each fin hinge axis. "
        "Force components come in the ROCKET frame (nose along +Z, so drag on "
        "a rocket flying nose-first is a negative F_z and lift from a "
        "positive angle of attack is +F_x) and, for reference, in the solver "
        "frame (body along +X, flow along +X). Quote the rocket frame to the "
        "operator."
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
    pivot_height_m: float | None = None,
    mpi_ranks: int = 10,
    max_iterations: int | None = None,
    convergence_residual: float = -5.0,
    turbulence_model: str = "SST",
    cfl_number: float | None = None,
    cfl_growth: float | None = None,
    cfl_max: float | None = None,
    convective_scheme: str | None = None,
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
        Angle of attack and sideslip in degrees, each within +/-90 (beyond
        +/-20 the result carries a note that it is only indicative).
    altitude_m:
        Geopotential altitude for the standard atmosphere.
    hinge_axes:
        Fin hinge definitions, each {"name": str, "point": [x,y,z],
        "direction": [u,v,w], "frame": "rocket"}, in the rocket frame (nose
        along +Z, origin at the nose tip) unless "frame" is "solver". The
        reported torque is the aerodynamic moment about the point, projected
        onto the direction.
    velocity_type, velocity_val format:
        Always a plain number plus its unit in velocity_type -- never a
        string such as "300 m/s" or "M2". Mach 0.8 is velocity_type='mach',
        velocity_val=0.8; 250 m/s true airspeed is velocity_type='tas',
        velocity_val=250. km/h and knots must be converted to m/s first
        (km/h / 3.6, kn * 0.5144).
    pivot_height_m:
        Moves the tilt axis (the pivot) up (+) or down (-) from the body
        axis, in metres, relative to the air at zero angle of attack: "up"
        is the side a positive angle of attack lifts the model towards
        (rocket +X). The flow is unchanged -- the solver tilts the air, not
        the model -- but pitching moments (cm_pitch) are taken about the
        pivot. Ignored when moment_origin is given.
    reference_area_m2, reference_length_m, moment_origin:
        Override the values measured from the CAD.
    mpi_ranks:
        MPI ranks for the solver; 10 suits a 6-core/12-thread machine.
    max_iterations, convergence_residual, turbulence_model:
        convergence_residual is the drop in log10 RMS[Rho] from its peak.
        max_iterations defaults to the operator's iteration limit
        (solver_max_iterations, 1000 unless changed); a run that reaches it
        is taken as the result, with a note. Change the default for good
        with update_settings. Solver controls.
    cfl_number:
        Starting CFL. Leave null to pick it from the flow regime: a cold
        supersonic start needs a far more cautious one than a subsonic run,
        and pinning a number here is how a Mach 1.3 case blows up at
        iteration six. A diverged run is retried automatically at first
        order, and the reply says so when that happened.
    cfl_growth, cfl_max:
        How fast the adaptive CFL grows per iteration (1.0-3.0) and where it
        stops. Null picks them from the regime. A low cfl_number alone is
        not a low CFL: the ramp decides where it goes, so to run cautiously
        lower cfl_max and keep cfl_growth near 1.05.
    convective_scheme:
        'JST', 'ROE', 'AUSM' or 'HLLC' to override the automatic choice
        (incompressible below Mach 0.3, JST to 0.8, Roe above). Naming one
        always solves compressible. Null keeps the automatic choice.
    """
    store = _store()
    try:
        mesh_path = store.resolve_mesh_path(mesh_id)
        mesh_record = store.get(mesh_id)
    except RecordNotFoundError as error:
        return _error(str(error), mesh_id=mesh_id)

    gate = _orientation_gate(
        _mesh_geometry_key(store, mesh_record), aoa_deg, sideslip_deg, mesh_id=mesh_id
    )
    if gate is not None:
        return gate

    try:
        axes = _hinge_axes(hinge_axes)
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
                max_iterations=max_iterations or _iteration_limit(),
                convergence_residual=convergence_residual,
                turbulence_model=turbulence_model,
                cfl_number=cfl_number,
                cfl_growth=cfl_growth,
                cfl_max=cfl_max,
                convective_scheme=(
                    ConvectiveScheme(convective_scheme.upper())
                    if convective_scheme
                    else None
                ),
            ),
            reference=ReferenceValues(
                reference_area_m2=reference_area_m2,
                reference_length_m=reference_length_m,
                moment_origin=_pivot_origin(moment_origin, pivot_height_m),
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
            on_iteration=_broadcast_iteration,
            on_line=_broadcast_line,
        )
    except Exception as error:
        store.update_metadata(record.record_id, {"failed": str(error)})
        return _error(str(error), sim_id=record.record_id)
    finally:
        _broadcast_finished()

    store.write_json(record.record_id, "result.json", result)
    store.update_metadata(
        record.record_id,
        {
            "cd": result.cd,
            "cl": result.cl,
            "mach": result.mach,
            "aoa_deg": result.aoa_deg,
        },
    )

    return _ok(
        sim_id=result.sim_id,
        mesh_id=mesh_id,
        mach=result.mach,
        speed_ms=result.speed_ms,
        aoa_deg=result.aoa_deg,
        sideslip_deg=result.sideslip_deg,
        dynamic_pressure_pa=result.dynamic_pressure_pa,
        # The rocket frame first, because it is the one the operator sees:
        # asked for "the force along each axis" of a rocket flying straight
        # up, the assistant used to quote the solver frame, where the body
        # lies along X, and call X the axial direction.
        axis_convention=AXIS_CONVENTION,
        forces_rocket_frame_n={
            "fx": result.force_rocket_n[0],
            "fy": result.force_rocket_n[1],
            "fz": result.force_rocket_n[2],
        },
        forces_solver_frame_n={
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
        center_of_pressure_rocket_frame=result.center_of_pressure_rocket,
        center_of_pressure_solver_frame=result.center_of_pressure,
        **_cop_from_nose(result.center_of_pressure_rocket, mesh_record),
        hinge_torques=[
            {
                "name": torque.name,
                "torque_nm": torque.torque_nm,
                "moment_vector_nm": torque.moment_vector_nm,
                "frame": torque.frame.value,
            }
            for torque in result.hinge_torques
        ],
        iterations=result.iterations,
        final_residual_rho=result.final_residual_rho,
        converged=result.converged,
        wall_time_s=result.wall_time_s,
        # An operator who is handed a silently rescued number will over-trust
        # it, so the assistant is told and asked to pass it on.
        rescued=result.rescued,
        notes=result.notes,
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
# Tool: radiation-shield study (DoE -> response surface -> Monte Carlo)
# ---------------------------------------------------------------------------


def _shield_params(
    base: Any,
    setup: dict[str, Any] | None,
    variables: dict[str, Any] | None,
    study: dict[str, Any] | None,
) -> Any:
    """Merge partial dictionaries onto a study definition and validate it."""
    from core.shield_models import ShieldStudyParams

    data = base.model_dump(mode="json") if base is not None else ShieldStudyParams().model_dump(mode="json")
    data["setup"].update(setup or {})
    for name, overrides in (variables or {}).items():
        if name not in ("wind_speed_ms", "solar_flux_w_m2", "bottom_flux_w_m2"):
            raise ValueError(
                f"unknown variable '{name}'; use wind_speed_ms, solar_flux_w_m2 "
                "or bottom_flux_w_m2"
            )
        data[name].update(overrides or {})
    data.update(study or {})
    return ShieldStudyParams.model_validate(data)


def _shield_summary(result: Any) -> dict[str, Any]:
    """The parts of a study result worth reading, flattened for the assistant."""
    mc = result.monte_carlo
    return {
        "study_id": result.study_id,
        "evaluator": result.evaluator.value,
        "design_points": [
            {
                "name": p.name,
                "wind_speed_ms": p.wind_speed_ms,
                "solar_flux_w_m2": p.solar_flux_w_m2,
                "bottom_flux_w_m2": p.bottom_flux_w_m2,
                "delta_t_k": p.delta_t_k,
            }
            for p in result.design_points
        ],
        "response_surface": {
            "kind": result.surrogate.kind.value,
            "points": result.surrogate.points,
            "r_squared": result.surrogate.r_squared,
            "leave_one_out_rmse_k": result.surrogate.loo_rmse_k,
        },
        "monte_carlo": {
            "samples": mc.samples,
            "mean_k": mc.mean_k,
            "std_k": mc.std_k,
            "p05_k": mc.p05_k,
            "p95_k": mc.p95_k,
            "p99_k": mc.p99_k,
            "abs_p95_k": mc.abs_p95_k,
            "worst_case": mc.worst_case,
            "reliability": mc.reliability,
            "tolerance_k": mc.tolerance_k,
            "sensitivities": mc.sensitivities,
        },
        "worst_case_rechecked_k": result.worst_case_check_k,
        "notes": result.notes,
    }


@server.tool(
    name="radiation_shield_study",
    description=(
        "Radiation-shield (thermometer screen) study after the reference "
        "methodology: a Design of Experiments over inlet wind speed, top solar "
        "radiation and bottom radiation, a response surface fitted to the "
        "solved points, and a Monte Carlo analysis on that surface giving the "
        "spread, the worst case and the reliability of T_monitor - T_inlet. "
        "action: 'analytic' (instant, analytical model), 'prepare_cfd' (mesh "
        "the 2 x 2 x 1.44 m domain, SU2 cases, Fluent/Workbench package), "
        "'solve_cfd' (run SU2 on the design points of study_id -- long), "
        "'import' (analyse design points solved elsewhere, e.g. Fluent, from "
        "results_csv), 'export_fluent', 'status'."
    ),
)
def radiation_shield_study(
    action: str = "analytic",
    study_id: str | None = None,
    setup: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
    study: dict[str, Any] | None = None,
    cfd: dict[str, Any] | None = None,
    results_csv: str | None = None,
    max_points: int | None = None,
) -> dict[str, Any]:
    """Run or continue a radiation-shield study.

    Parameters
    ----------
    action:
        'analytic', 'prepare_cfd', 'solve_cfd', 'import', 'export_fluent'
        or 'status'.
    study_id:
        An existing study ('shield-...'): required for solve_cfd,
        export_fluent and status; optional for import (its parameters are
        reused).
    setup:
        Overrides of the setup, any of: shield_step_path, scale_to_meters,
        shield_size_m [x,y,z], plate_count, domain_size_m [length along the
        wind, width, height] (default [2.0, 2.0, 1.44]), thermometer_xyz_m
        (monitor point relative to the shield centre), ambient_temp_c,
        wind_speed_ms, solar_flux_w_m2, bottom_mode ('ground_flux' or
        'roof_temperature'), bottom_flux_w_m2, bottom_temperature_k (343 K
        = 70 C roof), roof_emissivity, sky_longwave_w_m2, ground_albedo,
        shield_solar_absorptivity, shield_emissivity,
        shield_conductivity_w_mk, ventilation_coefficient.
    variables:
        Ranges and distributions of the three inputs, keyed
        'wind_speed_ms', 'solar_flux_w_m2', 'bottom_flux_w_m2', each
        {"minimum", "maximum", "distribution": uniform|normal|triangular,
        "mean", "std", "mode", "scale": linear|log}. Defaults: wind 0.5-5
        m/s (log), solar 800-1200 W/m2, bottom 300-800 W/m2, all uniform.
    study:
        doe ('ccd' face-centred, 15 points; or 'lhs'), doe_points (LHS),
        surrogate ('quadratic' or 'rbf'), monte_carlo_samples (default
        10000), tolerance_k (reliability threshold, default 0.5), seed.
    cfd:
        SU2 settings for prepare_cfd: mesh_resolution (coarse/medium/fine),
        turbulence_model (SST/SA; SU2 has no standard k-epsilon, the
        Fluent package uses it), buoyancy, radiation_passes,
        iterations_first_pass, iterations_later_passes,
        radiation_classes, rays_per_face, mpi_ranks.
    results_csv:
        For 'import': a CSV of solved design points (this program's
        design_points.csv with delta_t_k filled in, or a Workbench export).
    max_points:
        For 'solve_cfd': solve at most this many points in this call.
    """
    from backend import shield_workflow as workflow
    from core.shield_models import ShieldCfdSettings

    store = _store()
    action = (action or "").strip().lower()
    known = ("analytic", "prepare_cfd", "solve_cfd", "import", "export_fluent", "status")
    if action not in known:
        return _error(f"unknown action '{action}'; use {', '.join(known)}")
    try:
        base = workflow.load_params(store, study_id) if study_id else None
        params = _shield_params(base, setup, variables, study)
        settings = ShieldCfdSettings.model_validate(cfd or {})
    except RecordNotFoundError as error:
        return _error(str(error))
    except Exception as error:  # noqa: BLE001 - validation message is the point
        return _error(f"invalid parameters: {error}")

    try:
        if action == "analytic":
            result = workflow.run_analytic(store, params)
            return _ok(**_shield_summary(result))
        if action == "import":
            if not results_csv:
                return _error("action 'import' needs results_csv")
            result = workflow.analyse_imported(store, params, results_csv, study_id)
            return _ok(**_shield_summary(result))
        if action == "prepare_cfd":
            prepared = workflow.prepare_cfd(store, params, settings, on_line=_broadcast_line)
            return _ok(
                **prepared,
                next_step=(
                    "Run action 'solve_cfd' with this study_id to solve the design "
                    "points here, or run the script on the solving computer; the "
                    "Fluent package is in fluent_folder."
                ),
            )
        if not study_id:
            return _error(f"action '{action}' needs study_id")
        if action == "solve_cfd":
            summary = workflow.solve_cfd(
                store, study_id, _runner(), on_line=_broadcast_line, max_points=max_points
            )
            _broadcast_finished()
            result = summary.pop("result")
            if result is not None:
                summary.update(_shield_summary(result))
            return _ok(**summary)
        if action == "export_fluent":
            folder = workflow.export_package(store, study_id)
            return _ok(study_id=study_id, fluent_folder=str(folder))
        if action == "status":
            result = workflow.load_result(store, study_id)
            payload: dict[str, Any] = {"study_id": study_id, "params": params.model_dump(mode="json")}
            try:
                points = workflow.cfd_points(store, study_id)
                payload["cfd_points_solved"] = sum(p.delta_t_k is not None for p in points)
                payload["cfd_points_total"] = len(points)
            except Exception:  # noqa: BLE001 - not a CFD study
                pass
            if result is not None:
                payload.update(_shield_summary(result))
            return _ok(**payload)
    except Exception as error:  # noqa: BLE001 - reported to the caller
        return _error(str(error), study_id=study_id)
    return _error(f"unknown action '{action}'")  # pragma: no cover - listed above


# ---------------------------------------------------------------------------
# Tool 4: visualisation
# ---------------------------------------------------------------------------


@server.tool(
    name="generate_cfd_visualization",
    description=(
        "Render a publication-quality image from a completed simulation. "
        "Modes: 'surface_pressure' (body Cp or absolute pressure), "
        "'mach_slice' (cutting plane showing oblique shocks and "
        "Prandtl-Meyer expansion), 'schlieren' (density gradient on the "
        "cutting plane, like a wind-tunnel schlieren photograph -- the "
        "clearest picture of a shock wave, and the one to use when asked to "
        "show one), 'streamlines' (seeded flow paths coloured "
        "by velocity or temperature) and 'thermal' (temperature contours with "
        "the sensor marked). Saves a PNG and returns its path; the image "
        "also appears in the program's Graphics tab, where the operator can "
        "view and export it. Rocket results are drawn standing up, nose "
        "along +Z; for a Mach slice use camera_view 'side', which looks "
        "straight at the cutting plane."
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
    frame: str | None = None,
) -> dict[str, Any]:
    """Render a result image.

    Parameters
    ----------
    sim_id:
        Identifier of a completed simulation, or of one point of a sweep
        as the sweep lists it ("<sweep_id>-003"): every sweep point keeps
        its full solution, so there is no need to re-run it to draw it.
    visualization_type:
        'surface_pressure', 'mach_slice', 'schlieren', 'streamlines' or
        'thermal'.
    camera_view:
        'isometric', 'front', 'back', 'side', 'top', 'bottom',
        'nose_quarter' or 'tail_quarter'.
    slice_normal:
        Cutting-plane normal for slice modes, in the frame drawn; defaults
        to [0, 1, 0], the pitch plane an angle of attack tilts the rocket in.
    colormap:
        'turbo', 'coolwarm', 'viridis', 'jet', 'plasma' or 'inferno'.
    resolution:
        'preview', 'hd', '2k' or '4k'.
    output_path:
        Destination PNG; defaults to a file inside the run directory, which
        is where the Graphics tab looks. Give a path to export elsewhere.
    frame:
        'rocket' (nose along +Z, as the program shows it) or 'solver' (body
        along +X). Null picks 'rocket' for aerodynamic runs and 'solver'
        for thermal ones.
    """
    return render_run_image(
        _store(),
        sim_id,
        visualization_type=visualization_type,
        camera_view=camera_view,
        slice_normal=slice_normal,
        colormap=colormap,
        resolution=resolution,
        output_path=output_path,
        frame=frame,
    )


def render_run_image(
    store: RunStore,
    sim_id: str,
    visualization_type: str = "surface_pressure",
    camera_view: str = "isometric",
    slice_normal: list[float] | None = None,
    colormap: str = "turbo",
    resolution: str = "4k",
    output_path: str | None = None,
    frame: str | None = None,
) -> dict[str, Any]:
    """Render one image of a stored run, as the tool does.

    Shared with the Graphics tab, so a picture drawn from the interface and
    one the assistant asks for come out identical: same frame, same caption,
    same place in the registry.
    """
    solution_directory: Path | None = None
    point_label = ""
    try:
        record = store.get(sim_id)
    except RecordNotFoundError as error:
        # A sweep point: "<sweep_id>-003" lives in the sweep's point_003
        # folder, with a full volume solution of its own. The assistant used
        # to conclude that sweeps kept no solutions, and re-ran every point
        # as a separate simulation just to draw it.
        match = re.fullmatch(rf"({RUN_ID_PATTERN})-(\d{{3}})", sim_id)
        if not match:
            return _error(str(error), sim_id=sim_id)
        try:
            record = store.get(match.group(1))
        except RecordNotFoundError:
            return _error(str(error), sim_id=sim_id)
        solution_directory = record.path(f"point_{match.group(2)}")
        if not solution_directory.is_dir():
            return _error(f"sweep {match.group(1)} has no point {match.group(2)}", sim_id=sim_id)
        point_label = f"point_{match.group(2)}"

    frame = frame or ("rocket" if record.kind in ("aero", "sweep") else "solver")
    title = _render_title(record, visualization_type)
    if point_label:
        title = f"{title}  |  {point_label}"

    try:
        from backend.visualizer import render_visualization
    except Exception as error:  # pragma: no cover - optional dependency
        return _error(f"visualisation unavailable: {error}")

    stem = f"{point_label + '_' if point_label else ''}{visualization_type}_{camera_view}"
    destination = Path(output_path or record.path("renders", f"{stem}.png"))

    try:
        path = render_visualization(
            solution_directory or record.directory,
            visualization_type,
            destination,
            camera_view=camera_view,
            slice_normal=tuple(slice_normal or (0.0, 1.0, 0.0)),
            colormap=colormap,
            resolution=resolution,
            frame=frame,
            title=title,
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
        frame=frame,
        axis_convention=AXIS_CONVENTION if frame == "rocket" else None,
    )


def _render_title(record: Any, visualization_type: str) -> str:
    """A caption naming the case, so an exported image stands on its own."""
    # The flow condition is added by the renderer from the run's own solver
    # configuration, which sweep points have too.
    label = visualization_type.replace("_", " ")
    parts = [label, record.record_id]
    return "  |  ".join(parts)


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
    pivot_height_m: float | None = None,
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
    pivot_height_m:
        Moves the tilt axis (the pivot) up (+) or down (-) from the body
        axis, in metres, relative to the air at zero angle of attack: "up"
        is the side a positive angle of attack lifts the model towards
        (rocket +X). The flow is unchanged -- the solver tilts the air, not
        the model -- but pitching moments (cm_pitch) are taken about the
        pivot.
    """
    store = _store()
    try:
        mesh_path = store.resolve_mesh_path(mesh_id)
        mesh_record = store.get(mesh_id)
    except RecordNotFoundError as error:
        return _error(str(error), mesh_id=mesh_id)

    if not values:
        return _error("a sweep needs at least one value in 'values'")

    gate = _orientation_gate(_mesh_geometry_key(store, mesh_record), mesh_id=mesh_id)
    if gate is not None:
        return gate

    fixed = dict(fixed_params or {})
    try:
        axes = _hinge_axes(hinge_axes)
        base = AeroRunRequest(
            mesh_id=mesh_id,
            flow=FlowParams(
                velocity_type=VelocityType(fixed.get("velocity_type", "mach")),
                velocity_value=float(fixed.get("velocity_val", 2.0)),
                aoa_deg=float(fixed.get("aoa_deg", 0.0)),
                sideslip_deg=float(fixed.get("sideslip_deg", 0.0)),
                altitude_m=float(fixed.get("altitude_m", 0.0)),
            ),
            solver=SolverParams(
                mpi_ranks=mpi_ranks,
                max_iterations=int(fixed.get("max_iterations") or _iteration_limit()),
            ),
            reference=ReferenceValues(
                moment_origin=_pivot_origin(None, pivot_height_m)
            ),
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
            on_iteration=_broadcast_iteration,
            on_line=_broadcast_line,
        )
    except Exception as error:
        store.update_metadata(record.record_id, {"failed": str(error)})
        return _error(str(error), sweep_id=record.record_id)
    finally:
        _broadcast_finished()

    report = sweep.as_dict()
    for point in report["points"]:
        if point.get("succeeded"):
            point.update(_cop_from_nose(point.get("center_of_pressure_rocket"), mesh_record))
    store.write_json(record.record_id, "sweep.json", report)
    return _ok(sweep_id=record.record_id, **report)


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


@server.tool(
    name="get_active_geometry",
    description=(
        "Report the CAD file the operator currently has loaded in the "
        "desktop application: its path, the length unit read out of the "
        "file, the model's overall size, and which end of it the nose is "
        "on. Call this before asking the user for a path: when a STEP file "
        "has been imported in the interface, set_geometry_and_mesh can mesh "
        "it with no path at all. The 'ask_the_operator' list holds anything "
        "the geometry could not settle by itself -- put those questions to "
        "the user rather than guessing, because a wrong unit or a reversed "
        "nose produces confident, plausible, entirely wrong numbers. Also "
        "use this tool to record a file an agent was given, so the "
        "interface and later tool calls agree on the geometry in hand."
    ),
)
def get_active_geometry(
    step_file_path: str | None = None,
    nose_direction: str | None = None,
    scale_to_meters: float | None = None,
) -> dict[str, Any]:
    """Read, or set, the CAD file being worked on.

    Parameters
    ----------
    step_file_path:
        When given, record this file as the loaded one and report it.
        When omitted, report whatever is already loaded.
    nose_direction:
        Forward axis to record alongside the file.
    scale_to_meters:
        Override the unit. Omit to read the file's own declaration.
    """
    if step_file_path:
        record = set_active_geometry(
            step_file_path,
            scale_to_meters=scale_to_meters,
            nose_direction=nose_direction,
            source="mcp",
        )
        return _ok(
            loaded=True,
            geometry=record.as_dict(),
            ask_the_operator=record.open_questions(),
        )

    record = active_geometry()
    if record is None:
        return _ok(
            loaded=False,
            geometry=None,
            ask_the_operator=[],
            detail=(
                "no CAD file is loaded. The operator can import one in the "
                "Rocket Aerodynamics tab, or you can pass step_file_path to "
                "this tool or to set_geometry_and_mesh."
            ),
        )
    return _ok(
        loaded=True,
        geometry=record.as_dict(),
        ask_the_operator=record.open_questions(),
    )


# ---------------------------------------------------------------------------
# Project and settings tools
# ---------------------------------------------------------------------------


@server.tool(
    name="save_project",
    description=(
        "Save a complete simulation setup to a .atsproj project file: "
        "geometry, farfield domain, meshing, flight condition, solver "
        "settings, fin hinges, the sensor scenario and the sweep definition. "
        "Projects are plain JSON and open in the desktop GUI, so an agent can "
        "prepare a study and hand it to a human operator, or reload it in a "
        "later session. Omit a field to keep the default."
    ),
)
def save_project(
    name: str,
    path: str | None = None,
    description: str = "",
    author: str = "",
    notes: str = "",
    step_file_path: str | None = None,
    nose_direction: str = "+X",
    nose_vector: list[float] | None = None,
    reference_origin: list[float] | None = None,
    scale_to_meters: float = 1.0,
    domain_multipliers: dict[str, float] | None = None,
    domain_shape: str = "cylinder",
    mesh_resolution: str = "medium",
    boundary_layers: int = 7,
    target_yplus: float = 45.0,
    velocity_type: str = "mach",
    velocity_val: float = 2.0,
    aoa_deg: float = 0.0,
    sideslip_deg: float = 0.0,
    altitude_m: float = 0.0,
    hinge_axes: list[dict[str, Any]] | None = None,
    mpi_ranks: int = 10,
    sweep_parameter: str = "mach",
    sweep_values: list[float] | None = None,
    thermal: dict[str, Any] | None = None,
    mesh_id: str | None = None,
) -> dict[str, Any]:
    """Write a project file describing a complete setup.

    Parameters
    ----------
    name:
        Project name, also used for the filename when no path is given.
    path:
        Destination file. Defaults to the projects folder in the data
        directory, which is where the GUI looks first.
    description, author, notes:
        Provenance and working notes carried with the project.
    step_file_path, nose_direction, nose_vector, reference_origin,
    scale_to_meters:
        Geometry and orientation, as for set_geometry_and_mesh.
    domain_multipliers, domain_shape:
        Farfield envelope.
    mesh_resolution, boundary_layers, target_yplus:
        Meshing settings.
    velocity_type, velocity_val, aoa_deg, sideslip_deg, altitude_m:
        Flight condition.
    hinge_axes:
        Fin hinge definitions.
    mpi_ranks:
        Solver parallelism.
    sweep_parameter, sweep_values:
        Batch sweep definition stored with the project.
    thermal:
        Sensor scenario, e.g. {"vehicle_speed_ms": 2, "ambient_temp_c": 25,
        "solar_flux_w_m2": 800, "height_above_roof_m": 0.1,
        "sensor_xyz": [0.03, 0.02, 0.015]}.
    mesh_id:
        Mesh to associate with the project for reuse later.
    """
    try:
        project = default_project(name)
        project.metadata.description = description
        project.metadata.author = author
        project.notes = notes

        if step_file_path:
            project.geometry = GeometryParams(
                step_file_path=step_file_path,
                nose_direction=(
                    AxisDirection(nose_direction) if nose_vector is None else None
                ),
                nose_vector=nose_vector,
                reference_origin=reference_origin or [0.0, 0.0, 0.0],
                scale_to_meters=scale_to_meters,
            )

        multipliers = domain_multipliers or {}
        project.domain = DomainParams(
            shape=DomainShape(domain_shape),
            upstream_multiplier=float(multipliers.get("upstream", 5.0)),
            downstream_multiplier=float(multipliers.get("downstream", 10.0)),
            radial_multiplier=float(multipliers.get("radial", 5.0)),
        )
        project.mesh = MeshParams(
            resolution=MeshResolution(mesh_resolution),
            track=SimulationTrack.AERODYNAMIC,
            boundary_layers=boundary_layers,
            target_yplus=target_yplus,
        )
        project.flow = FlowParams(
            velocity_type=VelocityType(velocity_type),
            velocity_value=velocity_val,
            aoa_deg=aoa_deg,
            sideslip_deg=sideslip_deg,
            altitude_m=altitude_m,
        )
        project.solver = SolverParams(mpi_ranks=mpi_ranks)
        project.hinge_axes = _hinge_axes(hinge_axes)
        project.sweep = SweepSettings(
            parameter=sweep_parameter,
            values=sweep_values or [0.5, 1.0, 1.5, 2.0, 2.5],
        )
        if thermal is not None:
            project.thermal = ThermalParams(
                enclosure_step_path=thermal.get("enclosure_step_path", ""),
                vehicle_speed_ms=float(thermal.get("vehicle_speed_ms", 2.0)),
                ambient_temp_c=float(thermal.get("ambient_temp_c", 25.0)),
                solar_flux_w_m2=float(thermal.get("solar_flux_w_m2", 800.0)),
                height_above_roof_m=float(
                    thermal.get("height_above_roof_m", 0.1)
                ),
                sensor_xyz=thermal.get("sensor_xyz", [0.03, 0.02, 0.015]),
                housing_solar_absorptivity=float(
                    thermal.get("housing_solar_absorptivity", 0.30)
                ),
                roof_solar_absorptivity=float(
                    thermal.get("roof_solar_absorptivity", 0.65)
                ),
                housing_conductivity_w_mk=float(
                    thermal.get("housing_conductivity_w_mk", 0.18)
                ),
            )
        project.last_mesh_id = mesh_id
    except Exception as error:
        return _error(f"invalid parameters: {error}")

    destination = (
        Path(path)
        if path
        else projects_directory(_store().root) / f"{_safe_filename(name)}{PROJECT_EXTENSION}"
    )
    try:
        written = project.save(destination)
    except ProjectError as error:
        return _error(str(error))

    return _ok(project_path=str(written), **project.summary())


@server.tool(
    name="load_project",
    description=(
        "Read a .atsproj project file and return every setting it contains. "
        "Use it to continue a study prepared earlier or by a human operator "
        "in the GUI: the returned mesh_id, flow conditions and hinge axes can "
        "be passed straight to the simulation tools."
    ),
)
def load_project(path: str) -> dict[str, Any]:
    """Load a project file.

    Parameters
    ----------
    path:
        Path to the .atsproj file.
    """
    try:
        project = Project.load(path)
    except ProjectError as error:
        return _error(str(error), path=path)

    return _ok(
        project_path=str(Path(path).resolve()),
        summary=project.summary(),
        metadata=project.metadata.model_dump(mode="json"),
        geometry=(
            project.geometry.model_dump(mode="json") if project.geometry else None
        ),
        domain=project.domain.model_dump(mode="json"),
        mesh=project.mesh.model_dump(mode="json"),
        flow=project.flow.model_dump(mode="json"),
        solver=project.solver.model_dump(mode="json"),
        reference=project.reference.model_dump(mode="json"),
        hinge_axes=[axis.model_dump(mode="json") for axis in project.hinge_axes],
        thermal=(
            project.thermal.model_dump(mode="json") if project.thermal else None
        ),
        sweep=project.sweep.model_dump(mode="json"),
        notes=project.notes,
        last_mesh_id=project.last_mesh_id,
    )


@server.tool(
    name="list_saved_projects",
    description=(
        "List the project files in the data directory, newest first, with a "
        "one-line summary of each. Unreadable files are reported with their "
        "error rather than omitted, so a corrupted project is visible."
    ),
)
def list_saved_projects(directory: str | None = None) -> dict[str, Any]:
    """List available projects.

    Parameters
    ----------
    directory:
        Folder to scan. Defaults to the projects folder in the data
        directory, which is where the GUI saves.
    """
    target = Path(directory) if directory else projects_directory(_store().root)
    try:
        entries = _list_project_files(target)
    except Exception as error:
        return _error(str(error), directory=str(target))
    return _ok(directory=str(target), projects=entries, count=len(entries))


@server.tool(
    name="get_settings",
    description=(
        "Read the persistent application preferences: default MPI ranks, "
        "default colormap and resolutions, recent projects and interface "
        "behaviour. These are the same settings the GUI's Preferences dialog "
        "edits."
    ),
)
def get_settings() -> dict[str, Any]:
    """Return the current application settings."""
    settings = load_settings(_store().root, refresh=True)
    return _ok(settings_path=str(AppSettings.path(_store().root)), **settings.as_dict())


@server.tool(
    name="update_settings",
    description=(
        "Change persistent application preferences. Only the fields supplied "
        "are modified; everything else is left alone. The GUI picks the "
        "changes up the next time it reads its settings."
    ),
)
def update_settings(
    default_mpi_ranks: int | None = None,
    default_colormap: str | None = None,
    default_render_resolution: str | None = None,
    default_mesh_resolution: str | None = None,
    confirm_on_exit: bool | None = None,
    autosave_projects: bool | None = None,
    live_thermal_preview: bool | None = None,
    show_environment_warning: bool | None = None,
) -> dict[str, Any]:
    """Update selected preferences.

    Parameters
    ----------
    default_mpi_ranks:
        MPI ranks a new run starts with.
    default_colormap, default_render_resolution:
        Presentation defaults for renders.
    default_mesh_resolution:
        Mesh density a new project starts with.
    confirm_on_exit, autosave_projects, live_thermal_preview,
    show_environment_warning:
        Interface behaviour toggles.
    """
    settings = load_settings(_store().root, refresh=True)
    changes = {
        "default_mpi_ranks": default_mpi_ranks,
        "default_colormap": default_colormap,
        "default_render_resolution": default_render_resolution,
        "default_mesh_resolution": default_mesh_resolution,
        "confirm_on_exit": confirm_on_exit,
        "autosave_projects": autosave_projects,
        "live_thermal_preview": live_thermal_preview,
        "show_environment_warning": show_environment_warning,
    }
    applied = {key: value for key, value in changes.items() if value is not None}

    try:
        updated = settings.model_copy(update=applied)
        AppSettings(**updated.model_dump())  # revalidate the whole object
    except Exception as error:
        return _error(f"invalid settings: {error}")

    save_settings(updated, _store().root)
    return _ok(changed=applied, **updated.as_dict())


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

# Maps each registered tool name to the Python function behind it.
#
# The in-application AI helper (backend/ai_agent.py) drives exactly these
# capabilities, so it reads schemas from the MCP server's own registration and
# dispatches through this table. That keeps one definition serving three
# surfaces -- GUI, MCP and the built-in assistant -- rather than three that
# drift apart.
#
# The mapping is explicit rather than derived from function names because one
# of them differs: the tool registered as "run_parametric_sweep" is
# implemented by run_parametric_sweep_tool, since the plain name is already
# taken by the imported backend function. A test asserts this table matches
# what the server actually registered.
TOOL_FUNCTIONS: dict[str, Any] = {
    "set_geometry_and_mesh": set_geometry_and_mesh,
    "preview_orientation": preview_orientation,
    "run_aerodynamic_simulation": run_aerodynamic_simulation,
    "run_sensor_thermal_simulation": run_sensor_thermal_simulation,
    "radiation_shield_study": radiation_shield_study,
    "generate_cfd_visualization": generate_cfd_visualization,
    "run_parametric_sweep": run_parametric_sweep_tool,
    "list_runs": list_runs,
    "check_environment": check_environment,
    "get_active_geometry": get_active_geometry,
    "save_project": save_project,
    "load_project": load_project,
    "list_saved_projects": list_saved_projects,
    "get_settings": get_settings,
    "update_settings": update_settings,
}

# Tools that start a long computation. The assistant asks before running one
# of these, because a sweep can occupy the machine for half an hour and a user
# who typed a vague request should not discover that by waiting.
LONG_RUNNING_TOOLS = frozenset(
    {
        "set_geometry_and_mesh",
        "run_aerodynamic_simulation",
        "run_parametric_sweep",
    }
)

# Actions of a multi-purpose tool that are long, keyed by tool name.
LONG_RUNNING_ACTIONS: dict[str, frozenset[str]] = {
    "radiation_shield_study": frozenset({"prepare_cfd", "solve_cfd"}),
}


def is_long_running(name: str, arguments: dict[str, Any] | None = None) -> bool:
    """Whether a call starts a long computation the operator should approve."""
    if name in LONG_RUNNING_TOOLS:
        return True
    actions = LONG_RUNNING_ACTIONS.get(name)
    if actions is None:
        return False
    return str((arguments or {}).get("action", "")).lower() in actions


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke a registered tool by name.

    Parameters
    ----------
    name:
        Registered tool name, as it appears in the MCP tool list.
    arguments:
        Keyword arguments for the tool.

    Returns
    -------
    dict
        The tool's own structured response, or a structured error when the
        name is unknown or the arguments do not fit. Tools already return
        errors rather than raising, so a caller never has to catch.
    """
    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        return _error(
            f"unknown tool '{name}'; available tools: "
            f"{', '.join(sorted(TOOL_FUNCTIONS))}"
        )
    try:
        return function(**arguments)
    except TypeError as error:
        return _error(f"wrong arguments for '{name}': {error}")
    except Exception as error:  # noqa: BLE001 - reported, never raised onward
        return _error(f"'{name}' failed: {type(error).__name__}: {error}")


def _safe_filename(name: str) -> str:
    """Turn a project name into a filename safe on Windows and POSIX."""
    keep = [
        character if (character.isalnum() or character in "-_ ") else "_"
        for character in name.strip()
    ]
    cleaned = "".join(keep).strip().replace(" ", "_")
    return cleaned or "project"


def main() -> None:
    """Run the MCP server on stdio."""
    server.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover - entry point
    main()
