"""MCP server: tool registration, schemas and end-to-end workflows.

The point of this layer is that an AI agent can reach every capability the GUI
offers. These tests therefore check both halves: that the schemas an agent
reads are complete and self-describing, and that the tools actually drive a
workflow through to a result.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")

import mcp_server as server_module  # noqa: E402
from backend.runner import FakeRunner  # noqa: E402
from core.store import RunStore  # noqa: E402

# The five tools named in the specification, plus the supporting ones.
SPECIFIED_TOOLS = {
    "set_geometry_and_mesh",
    "run_aerodynamic_simulation",
    "run_sensor_thermal_simulation",
    "generate_cfd_visualization",
    "run_parametric_sweep",
}

SUPPORTING_TOOLS = {
    "list_runs",
    "check_environment",
    "get_active_geometry",
    "save_project",
    "load_project",
    "list_saved_projects",
    "get_settings",
    "update_settings",
}

EXPECTED_TOOLS = SPECIFIED_TOOLS | SUPPORTING_TOOLS

# Recorded SU2 screen output, as in the aero solver tests.
SCREEN_HEADER = (
    "|  Inner_Iter|    rms[Rho]|   rms[RhoE]|          CL|          CD|"
    "        CSF|        CMx|        CMy|        CMz|"
)

FORCES_BREAKDOWN = """
Total CL:       0.250000 | Pressure (  95%):   0.237500 | Friction (   5%): 0.012500
Total CD:       0.420000 | Pressure (  70%):   0.294000 | Friction (  30%): 0.126000
Total CSF:      0.000000 | Pressure (   0%):   0.000000 | Friction (   0%): 0.000000
Total CMx:      0.000000 | Pressure (   0%):   0.000000 | Friction (   0%): 0.000000
Total CMy:     -0.050000 | Pressure (  90%):  -0.045000 | Friction (  10%): -0.005000
Total CMz:      0.000000 | Pressure (   0%):   0.000000 | Friction (   0%): 0.000000
Total CFx:      0.410000 | Pressure (  70%):   0.287000 | Friction (  30%): 0.123000
Total CFy:      0.000000 | Pressure (   0%):   0.000000 | Friction (   0%): 0.000000
Total CFz:      0.260000 | Pressure (  95%):   0.247000 | Friction (   5%): 0.013000
"""


def screen_rows(count: int = 80) -> list[str]:
    """Synthetic converging solver output."""
    lines = ["+" + "-" * 70 + "+", SCREEN_HEADER, "+" + "-" * 70 + "+"]
    for index in range(count):
        residual = -1.0 - 5.0 * (index + 1) / count
        lines.append(
            "|{:12d}|{:12.6f}|{:12.6f}|{:12.6f}|{:12.6f}|{:12.6f}"
            "|{:12.6f}|{:12.6f}|{:12.6f}|".format(
                index, residual, residual - 1.0, 0.25, 0.42, 0.0, 0.0, -0.05, 0.0
            )
        )
    return lines


@pytest.fixture(autouse=True)
def fake_solver():
    """Install a recorded-output solver for the duration of each test."""
    server_module.set_runner(
        FakeRunner(
            lines=screen_rows(),
            artifacts={"forces_breakdown.dat": FORCES_BREAKDOWN},
        )
    )
    yield
    server_module.set_runner(None)


@pytest.fixture
def prepared_mesh(store: RunStore) -> str:
    """A mesh record standing in for one the meshing tool produced."""
    record = store.create("mesh", {"step_file_path": "/models/rocket.step"})
    record.path("mesh.su2").write_text("NDIME= 3\nNELEM= 0\nNPOIN= 0\nNMARK= 0\n")
    store.update_metadata(
        record.record_id,
        {
            "mesh_path": str(record.path("mesh.su2")),
            "cell_count": 310_000,
            "reference_area_m2": 0.005,
            "reference_length_m": 0.08,
        },
    )
    return record.record_id


def call(tool, **kwargs):
    """Invoke a tool function directly, awaiting it when needed."""
    result = tool(**kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.get_event_loop().run_until_complete(result)
    return result


# ---------------------------------------------------------------------------
# Registration and schemas
# ---------------------------------------------------------------------------


def test_every_specified_tool_is_registered():
    """Every specified tool, and nothing unexpected, is exposed."""
    tools = asyncio.run(server_module.server.list_tools())
    names = {tool.name for tool in tools}
    assert SPECIFIED_TOOLS <= names, f"missing: {SPECIFIED_TOOLS - names}"
    assert names == EXPECTED_TOOLS


def test_the_direct_call_registry_matches_the_registered_tools():
    """The in-program assistant dispatches through TOOL_FUNCTIONS.

    A tool registered with the MCP server but absent here would be offered to
    the assistant and then fail as unknown, so the two must not drift.
    """
    tools = asyncio.run(server_module.server.list_tools())
    assert {tool.name for tool in tools} == set(server_module.TOOL_FUNCTIONS)


def test_calling_an_unknown_tool_returns_a_structured_error():
    result = server_module.call_tool("no_such_tool", {})
    assert result["ok"] is False
    assert "unknown tool" in result["error"]


def test_calling_a_tool_with_wrong_arguments_does_not_raise():
    result = server_module.call_tool("check_environment", {"nonexistent": 1})
    assert result["ok"] is False


def test_a_tool_can_be_called_directly_by_name():
    result = server_module.call_tool("check_environment", {})
    assert result["ok"] is True


def test_long_running_tools_are_registered_tools():
    """A stale name here would disable a confirmation prompt silently."""
    assert server_module.LONG_RUNNING_TOOLS <= set(server_module.TOOL_FUNCTIONS)


# ---------------------------------------------------------------------------
# The geometry hand-off
# ---------------------------------------------------------------------------


def loaded_rocket(tmp_path, **shape):
    """A millimetre rocket, as an exporter would write one."""
    from tests.test_step_inspect import MILLIMETRES, rocket_points, write_step

    return write_step(
        tmp_path / "Sapphire.step", MILLIMETRES, rocket_points(**shape)
    )


def capture_geometry(monkeypatch):
    """Stop the pipeline at the door and report what it was handed."""
    seen = {}

    def fake_generate(request, output_path):
        seen["step_file_path"] = request.geometry.step_file_path
        seen["scale"] = request.geometry.scale_to_meters
        seen["nose"] = request.geometry.nose_direction
        raise RuntimeError("stopped before meshing")

    monkeypatch.setattr(server_module, "generate_mesh", fake_generate)
    return seen


def test_the_nose_direction_is_read_off_the_shape(tmp_path, monkeypatch):
    """An agent that does not say which way the rocket points gets it right.

    The detector answers in the meshing convention: nose-to-tail, which for
    a model whose tip is at +X is '-X'.
    """
    seen = capture_geometry(monkeypatch)
    call(server_module.set_geometry_and_mesh, step_file_path=loaded_rocket(tmp_path))
    assert seen["nose"].value == "-X"


def test_an_explicit_nose_direction_is_obeyed(tmp_path, monkeypatch):
    """The caller has the last word, even against the shape."""
    seen = capture_geometry(monkeypatch)
    call(
        server_module.set_geometry_and_mesh,
        step_file_path=loaded_rocket(tmp_path),
        nose_direction="+Z",
    )
    assert seen["nose"].value == "+Z"


def test_a_body_with_no_obvious_nose_asks_instead_of_guessing(tmp_path, monkeypatch):
    """Meshing a rocket backwards is silent and ruinous, so it is refused.

    The reply has to be actionable: which axis, and what to pass once the
    operator has answered.
    """
    capture_geometry(monkeypatch)
    reply = call(
        server_module.set_geometry_and_mesh,
        step_file_path=loaded_rocket(tmp_path, nose_radius=90.0),
    )
    assert reply["ok"] is False
    assert "which end is the nose" in reply["error"]
    assert reply["needs"] == ["nose_direction"]
    assert reply["axis"] == "X"
    assert "+X" in reply["error"] and "-X" in reply["error"]


def test_the_operators_own_choice_beats_the_shape(tmp_path, monkeypatch):
    """A direction set in the interface is not re-derived behind their back."""
    from core.workspace import set_active_geometry

    path = loaded_rocket(tmp_path)
    set_active_geometry(path, nose_direction="+Y", source="gui")

    seen = capture_geometry(monkeypatch)
    call(server_module.set_geometry_and_mesh)
    assert seen["nose"].value == "+Y"


def test_the_reply_says_which_nose_direction_was_used(tmp_path, monkeypatch):
    """The operator should be able to check the assumption after the fact."""
    def fake_generate(request, output_path):
        raise RuntimeError("stopped")

    monkeypatch.setattr(server_module, "generate_mesh", fake_generate)
    reply = call(
        server_module.set_geometry_and_mesh, step_file_path=loaded_rocket(tmp_path)
    )
    # The mesh failed, but the resolution happened before that and the
    # geometry is on the bench for the next call.
    from core.workspace import active_geometry

    assert active_geometry().nose_direction == "-X"
    assert reply["ok"] is False


def test_the_assistant_can_see_that_nothing_is_loaded():
    """An empty bench is reported, with what to do about it."""
    reply = call(server_module.get_active_geometry)
    assert reply["ok"] is True
    assert reply["loaded"] is False
    assert "step_file_path" in reply["detail"]


def test_the_assistant_reads_back_the_file_the_operator_imported(tmp_path):
    """The whole point: no path is typed twice."""
    from core.workspace import set_active_geometry

    path = loaded_rocket(tmp_path)
    set_active_geometry(path, nose_direction="+Y", source="gui")

    reply = call(server_module.get_active_geometry)
    assert reply["loaded"] is True
    geometry = reply["geometry"]
    assert geometry["step_file_path"] == path
    assert geometry["nose_direction"] == "+Y"
    assert geometry["units"] == "millimetres"
    assert geometry["largest_extent_m"] == pytest.approx(1.32)
    assert reply["ask_the_operator"] == []


def test_an_uncertain_geometry_hands_the_assistant_the_question(tmp_path):
    """What the assistant should ask, rather than what it should assume."""
    from core.workspace import set_active_geometry

    set_active_geometry(loaded_rocket(tmp_path, nose_radius=90.0), source="gui")
    reply = call(server_module.get_active_geometry)

    assert reply["loaded"] is True
    questions = reply["ask_the_operator"]
    assert len(questions) == 1
    assert "which end" in questions[0].lower()


@pytest.mark.parametrize("with_file", [True, False])
def test_every_reply_carries_the_question_list(tmp_path, with_file):
    """A field that is sometimes absent is a field nobody reads."""
    if with_file:
        from core.workspace import set_active_geometry

        set_active_geometry(loaded_rocket(tmp_path), source="gui")
    reply = call(server_module.get_active_geometry)
    assert "ask_the_operator" in reply


def test_an_agent_can_put_a_file_on_the_bench_itself(tmp_path):
    """A path an agent was given becomes visible in the interface too."""
    from core.workspace import active_geometry

    path = loaded_rocket(tmp_path)
    reply = call(server_module.get_active_geometry, step_file_path=path)

    assert reply["loaded"] is True
    assert reply["geometry"]["scale_to_meters"] == pytest.approx(1e-3)
    stored = active_geometry()
    assert stored is not None and stored.step_file_path == path
    assert stored.source == "mcp"


def test_meshing_without_a_path_and_without_a_loaded_file_says_what_to_do():
    """The failure an agent can act on, rather than a traceback."""
    reply = call(server_module.set_geometry_and_mesh)
    assert reply["ok"] is False
    assert "no STEP file given" in reply["error"]
    assert "get_active_geometry" in reply["error"]


def test_meshing_without_a_path_uses_the_loaded_file(tmp_path, monkeypatch):
    """'Mesh what I just imported' reaches the mesher with the right file."""
    from core.workspace import set_active_geometry

    path = loaded_rocket(tmp_path)
    set_active_geometry(path, nose_direction="+Y", source="gui")

    seen = {}

    def fake_generate(request, output_path):
        seen["step_file_path"] = request.geometry.step_file_path
        seen["scale"] = request.geometry.scale_to_meters
        seen["nose"] = request.geometry.nose_direction
        raise RuntimeError("stopped before meshing")

    monkeypatch.setattr(server_module, "generate_mesh", fake_generate)
    call(server_module.set_geometry_and_mesh)

    assert seen["step_file_path"] == path
    # The unit came from the file, not from the 1.0 default.
    assert seen["scale"] == pytest.approx(1e-3)
    assert seen["nose"].value == "+Y"


def test_an_explicit_path_still_has_its_units_read(tmp_path, monkeypatch):
    """An agent that names a file should not also have to name its unit."""
    path = loaded_rocket(tmp_path)
    seen = {}

    def fake_generate(request, output_path):
        seen["scale"] = request.geometry.scale_to_meters
        raise RuntimeError("stopped before meshing")

    monkeypatch.setattr(server_module, "generate_mesh", fake_generate)
    call(server_module.set_geometry_and_mesh, step_file_path=path)
    assert seen["scale"] == pytest.approx(1e-3)


def test_an_explicit_scale_overrides_the_file(tmp_path, monkeypatch):
    """The operator has the last word on units."""
    path = loaded_rocket(tmp_path)
    seen = {}

    def fake_generate(request, output_path):
        seen["scale"] = request.geometry.scale_to_meters
        raise RuntimeError("stopped before meshing")

    monkeypatch.setattr(server_module, "generate_mesh", fake_generate)
    call(
        server_module.set_geometry_and_mesh,
        step_file_path=path,
        scale_to_meters=0.0254,
    )
    assert seen["scale"] == pytest.approx(0.0254)


def test_meshing_an_explicit_path_puts_it_on_the_bench(tmp_path, monkeypatch):
    """After an agent meshes a file, the interface shows that file."""
    from core.workspace import active_geometry

    path = loaded_rocket(tmp_path)
    monkeypatch.setattr(
        server_module,
        "generate_mesh",
        lambda request, output_path: (_ for _ in ()).throw(RuntimeError("stop")),
    )
    call(server_module.set_geometry_and_mesh, step_file_path=path)

    stored = active_geometry()
    assert stored is not None and stored.step_file_path == path


def test_tools_carry_thorough_descriptions():
    """An agent picks a tool from its description, so it must be substantial."""
    tools = asyncio.run(server_module.server.list_tools())
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"
        assert len(tool.description) > 80, f"{tool.name} description is too thin"


def test_tool_schemas_expose_the_documented_parameters():
    """The parameters named in the specification are all reachable."""
    tools = {tool.name: tool for tool in asyncio.run(server_module.server.list_tools())}

    mesh_params = set(tools["set_geometry_and_mesh"].input_schema["properties"])
    assert {"step_file_path", "nose_vector", "domain_multipliers", "mesh_resolution"} <= mesh_params

    aero_params = set(tools["run_aerodynamic_simulation"].input_schema["properties"])
    assert {
        "mesh_id", "velocity_type", "velocity_val", "aoa_deg",
        "sideslip_deg", "altitude_m", "hinge_axes",
    } <= aero_params

    thermal_params = set(
        tools["run_sensor_thermal_simulation"].input_schema["properties"]
    )
    assert {
        "enclosure_step", "vehicle_speed_ms", "ambient_temp_c",
        "solar_flux_w_m2", "height_above_roof_m", "sensor_xyz",
    } <= thermal_params

    viz_params = set(tools["generate_cfd_visualization"].input_schema["properties"])
    assert {"sim_id", "visualization_type", "camera_view", "slice_normal"} <= viz_params

    sweep_params = set(tools["run_parametric_sweep"].input_schema["properties"])
    assert {"param_name", "values", "fixed_params"} <= sweep_params


def test_schemas_are_json_serialisable():
    """Schemas must survive the wire protocol."""
    import json

    tools = asyncio.run(server_module.server.list_tools())
    for tool in tools:
        json.dumps(tool.input_schema)


# ---------------------------------------------------------------------------
# Aerodynamic workflow
# ---------------------------------------------------------------------------


def test_aerodynamic_simulation_returns_forces_and_torques(prepared_mesh):
    """The headline tool returns everything the specification lists."""
    response = call(
        server_module.run_aerodynamic_simulation,
        mesh_id=prepared_mesh,
        velocity_type="mach",
        velocity_val=2.0,
        aoa_deg=5.0,
        mpi_ranks=1,
        hinge_axes=[
            {"name": "fin_1", "point": [0.9, 0.05, 0.0], "direction": [0, 1, 0]}
        ],
    )

    assert response["ok"], response.get("error")
    assert response["coefficients"]["cd"] == pytest.approx(0.42)
    assert response["coefficients"]["cl"] == pytest.approx(0.25)
    assert response["mach"] == pytest.approx(2.0)
    assert set(response["forces_rocket_frame_n"]) == {"fx", "fy", "fz"}
    assert set(response["forces_solver_frame_n"]) == {"fx", "fy", "fz"}
    assert len(response["center_of_pressure_rocket_frame"]) == 3
    assert "+Z" in response["axis_convention"]
    # One force, two frames: the solver's downstream X is the rocket's -Z.
    assert response["forces_rocket_frame_n"]["fz"] == pytest.approx(
        -response["forces_solver_frame_n"]["fx"]
    )
    assert response["forces_rocket_frame_n"]["fx"] == pytest.approx(
        response["forces_solver_frame_n"]["fz"]
    )
    assert response["hinge_torques"][0]["name"] == "fin_1"
    assert math.isfinite(response["hinge_torques"][0]["torque_nm"])
    assert response["sim_id"]


def test_a_rescued_run_tells_the_assistant_it_was_rescued(prepared_mesh):
    """An assistant that is not told cannot pass it on.

    A rescued number is valid but the case needed help to reach it, and the
    operator hears about the run only through the assistant's answer.
    """
    response = call(
        server_module.run_aerodynamic_simulation,
        mesh_id=prepared_mesh,
        velocity_val=2.0,
        mpi_ranks=1,
    )
    assert response["ok"], response.get("error")
    # This run converged first time, so the fields are present and quiet.
    assert response["rescued"] is False
    assert response["notes"] == []


def test_the_cfl_number_defaults_to_the_regime(prepared_mesh):
    """Pinning 5.0 at Mach 1.3 is what made a solve diverge at iteration six."""
    schema = asyncio.run(server_module.server.list_tools())
    tool = next(t for t in schema if t.name == "run_aerodynamic_simulation")
    cfl = tool.input_schema["properties"]["cfl_number"]
    assert cfl.get("default") is None


def test_true_airspeed_is_accepted(prepared_mesh):
    """velocity_type='tas' interprets the value as m/s."""
    response = call(
        server_module.run_aerodynamic_simulation,
        mesh_id=prepared_mesh,
        velocity_type="tas",
        velocity_val=340.294,
        mpi_ranks=1,
    )
    assert response["ok"]
    assert response["mach"] == pytest.approx(1.0, rel=1e-3)


def test_unknown_mesh_id_is_reported_not_raised(store):
    """Agents get a structured error they can recover from."""
    response = call(
        server_module.run_aerodynamic_simulation, mesh_id="mesh-nope", mpi_ranks=1
    )
    assert response["ok"] is False
    assert "mesh-nope" in response["error"]


def test_out_of_range_parameters_are_rejected_with_an_explanation(prepared_mesh):
    """The model bounds surface as an actionable message."""
    response = call(
        server_module.run_aerodynamic_simulation,
        mesh_id=prepared_mesh,
        velocity_val=2.0,
        aoa_deg=45.0,  # outside the +/-20 envelope
        mpi_ranks=1,
    )
    assert response["ok"] is False
    assert "invalid parameters" in response["error"]


def test_results_are_persisted_for_later_retrieval(prepared_mesh, store):
    """A run is recorded so it can be visualised or listed afterwards."""
    response = call(
        server_module.run_aerodynamic_simulation,
        mesh_id=prepared_mesh,
        velocity_val=2.0,
        mpi_ranks=1,
    )
    record = store.get(response["sim_id"])
    assert record.kind == "aero"
    assert store.read_json(response["sim_id"], "result.json")["cd"] == pytest.approx(
        0.42
    )


# ---------------------------------------------------------------------------
# Thermal workflow
# ---------------------------------------------------------------------------


def test_thermal_analytic_mode_needs_no_mesh():
    """An instant estimate is available before any meshing."""
    response = call(
        server_module.run_sensor_thermal_simulation,
        vehicle_speed_ms=2.0,
        ambient_temp_c=25.0,
        solar_flux_w_m2=800.0,
        height_above_roof_m=0.1,
        sensor_xyz=[0.03, 0.02, 0.015],
        analytic_only=True,
    )
    assert response["ok"]
    assert response["method"] == "analytic"
    assert response["ambient_temp_c"] == pytest.approx(25.0)
    assert 0.0 < response["delta_t_error_k"] < 6.0
    assert 40.0 < response["roof_temp_c"] < 90.0
    assert response["intake_in_roof_plume"] is False


def test_thermal_reports_the_measurement_bias_consistently():
    """delta_t is exactly sensor minus ambient."""
    response = call(
        server_module.run_sensor_thermal_simulation,
        ambient_temp_c=30.0,
        analytic_only=True,
    )
    assert response["delta_t_error_k"] == pytest.approx(
        response["sensor_temp_c"] - 30.0, abs=1e-9
    )


def test_thermal_responds_to_shading():
    """Removing the sun must reduce the reported bias."""
    sunny = call(
        server_module.run_sensor_thermal_simulation,
        solar_flux_w_m2=900.0,
        analytic_only=True,
    )
    shaded = call(
        server_module.run_sensor_thermal_simulation,
        solar_flux_w_m2=0.0,
        analytic_only=True,
    )
    assert shaded["delta_t_error_k"] < sunny["delta_t_error_k"]


def test_thermal_conjugate_run_uses_the_mesh(prepared_mesh):
    """Supplying a mesh_id runs the conjugate solve path."""
    response = call(
        server_module.run_sensor_thermal_simulation,
        mesh_id=prepared_mesh,
        sensor_xyz=[0.03, 0.02, 0.015],
        mpi_ranks=1,
    )
    assert response["ok"], response.get("error")
    assert response["method"] == "conjugate_cfd"
    assert "analytic_delta_t_error_k" in response


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def test_sweep_runs_every_point_and_returns_curves(prepared_mesh):
    """A Mach sweep yields one point per value plus summary curves."""
    response = call(
        server_module.run_parametric_sweep_tool,
        mesh_id=prepared_mesh,
        param_name="mach",
        values=[0.5, 1.5, 2.5],
        fixed_params={"aoa_deg": 3.0},
        hinge_axes=[
            {"name": "fin_1", "point": [0.9, 0.05, 0.0], "direction": [0, 1, 0]}
        ],
        mpi_ranks=1,
    )

    assert response["ok"], response.get("error")
    assert response["succeeded"] == 3
    assert response["failed"] == 0
    assert response["curves"]["values"] == [0.5, 1.5, 2.5]
    assert len(response["curves"]["cd"]) == 3
    # Every point held the fixed angle of attack.
    assert all(point["aoa_deg"] == 3.0 for point in response["points"])


def test_sweep_reports_the_servo_sizing_torque(prepared_mesh):
    """The peak hinge torque across the sweep is surfaced explicitly."""
    response = call(
        server_module.run_parametric_sweep_tool,
        mesh_id=prepared_mesh,
        param_name="aoa",
        values=[0.0, 5.0, 10.0],
        hinge_axes=[
            {"name": "fin_1", "point": [0.9, 0.05, 0.0], "direction": [0, 1, 0]}
        ],
        mpi_ranks=1,
    )
    assert response["ok"]
    assert "servo_sizing_torque_nm" in response["extremes"]
    assert response["extremes"]["servo_sizing_torque_nm"] >= 0.0


def test_sweep_rejects_an_unsweepable_parameter(prepared_mesh):
    """An unknown parameter names the ones that are supported."""
    response = call(
        server_module.run_parametric_sweep_tool,
        mesh_id=prepared_mesh,
        param_name="wing_area",
        values=[1.0],
        mpi_ranks=1,
    )
    assert response["ok"] is False
    assert "not a sweepable parameter" in response["error"]


def test_sweep_requires_values(prepared_mesh):
    """An empty sweep is a mistake worth catching."""
    response = call(
        server_module.run_parametric_sweep_tool,
        mesh_id=prepared_mesh,
        param_name="mach",
        values=[],
    )
    assert response["ok"] is False


# ---------------------------------------------------------------------------
# Supporting tools
# ---------------------------------------------------------------------------


def test_list_runs_surfaces_stored_records(prepared_mesh, store):
    """An agent can rediscover ids from an earlier session."""
    response = call(server_module.list_runs, kind="mesh")
    assert response["ok"]
    assert any(r["record_id"] == prepared_mesh for r in response["records"])


def test_check_environment_reports_the_toolchain():
    """The environment probe is reachable over MCP."""
    response = call(server_module.check_environment)
    assert response["ok"]
    assert "solver_ready" in response
    assert "cpu_count" in response


def test_visualization_reports_a_missing_solution(prepared_mesh):
    """Rendering a run with no solution file fails cleanly."""
    response = call(
        server_module.generate_cfd_visualization,
        sim_id=prepared_mesh,
        visualization_type="mach_slice",
    )
    assert response["ok"] is False
    assert "solution" in response["error"].lower()


def test_visualization_renders_from_a_stored_solution(store):
    """A completed run renders through the MCP tool."""
    pv = pytest.importorskip("pyvista")
    import numpy as np

    record = store.create("aero", {})
    grid = pv.ImageData(dimensions=(12, 10, 10), spacing=(0.05, 0.02, 0.02))
    grid = grid.cast_to_unstructured_grid()
    grid.point_data["Mach"] = np.linspace(0.5, 2.5, grid.n_points)
    grid.point_data["Pressure"] = np.linspace(9e4, 1.1e5, grid.n_points)
    grid.save(str(record.path("flow.vtu")))

    response = call(
        server_module.generate_cfd_visualization,
        sim_id=record.record_id,
        visualization_type="mach_slice",
        resolution="preview",
        colormap="viridis",
    )
    assert response["ok"], response.get("error")
    assert Path(response["image_path"]).is_file()
    # A rocket is drawn standing up, and the reply says which way is which.
    assert response["frame"] == "rocket"
    assert "+Z" in response["axis_convention"]
    # In the run's renders folder, which is where the Graphics tab looks.
    assert Path(response["image_path"]).parent == record.path("renders")


def test_visualization_frame_can_be_chosen(store, monkeypatch):
    """The solver frame stays available, and thermal runs default to it."""
    import backend.visualizer as visualizer

    seen = []

    def fake_render(solution, kind, output_path, **kwargs):
        seen.append((kwargs["frame"], kwargs["title"]))
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"png")
        return Path(output_path)

    monkeypatch.setattr(visualizer, "render_visualization", fake_render)
    aero = store.create("aero", {"mach": 1.3, "aoa_deg": 4.0})
    thermal = store.create("thermal", {})

    call(server_module.generate_cfd_visualization, sim_id=aero.record_id)
    call(server_module.generate_cfd_visualization, sim_id=aero.record_id, frame="solver")
    call(server_module.generate_cfd_visualization, sim_id=thermal.record_id,
         visualization_type="thermal")

    assert [frame for frame, _ in seen] == ["rocket", "solver", "solver"]
    # The caption names the case, so an exported image stands on its own.
    assert "M 1.30" in seen[0][1] and "alpha 4" in seen[0][1]
    assert aero.record_id in seen[0][1]


def test_hinges_from_the_assistant_are_in_rocket_axes():
    """What the operator is shown is what the assistant is asked in."""
    from core.frames import Frame

    default, explicit = server_module._hinge_axes(
        [
            {"point": [0.0, 0.05, -0.9], "direction": [0, 1, 0]},
            {"point": [0.9, 0.05, 0.0], "direction": [0, 1, 0], "frame": "solver"},
        ]
    )
    assert default.frame is Frame.ROCKET
    assert explicit.frame is Frame.SOLVER
    # The same physical hinge either way.
    assert default.solver_point() == pytest.approx(explicit.solver_point())


# ---------------------------------------------------------------------------
# Projects and settings over MCP
# ---------------------------------------------------------------------------


def test_agent_can_save_and_reload_a_project(store, tmp_path):
    """An agent prepares a study and a human opens it in the GUI."""
    path = tmp_path / "agent.atsproj"
    saved = call(
        server_module.save_project,
        name="Agent study",
        path=str(path),
        description="Mach sweep for fin sizing",
        step_file_path="/models/rocket.step",
        aoa_deg=4.0,
        velocity_val=2.2,
        hinge_axes=[
            {"name": "fin_1", "point": [0.9, 0.05, 0.0], "direction": [0, 1, 0]}
        ],
        sweep_parameter="mach",
        sweep_values=[0.8, 1.6, 2.4],
        thermal={"vehicle_speed_ms": 3.0, "solar_flux_w_m2": 900.0},
    )
    assert saved["ok"], saved.get("error")
    assert Path(saved["project_path"]).is_file()

    loaded = call(server_module.load_project, path=saved["project_path"])
    assert loaded["ok"]
    assert loaded["metadata"]["name"] == "Agent study"
    assert loaded["flow"]["aoa_deg"] == pytest.approx(4.0)
    assert loaded["sweep"]["values"] == [0.8, 1.6, 2.4]
    assert loaded["thermal"]["vehicle_speed_ms"] == pytest.approx(3.0)
    assert loaded["hinge_axes"][0]["name"] == "fin_1"


def test_saved_project_opens_in_the_core_loader(store, tmp_path):
    """What the agent writes is a real project file, not a lookalike."""
    from core.project import Project

    saved = call(
        server_module.save_project,
        name="Round trip",
        path=str(tmp_path / "rt.atsproj"),
        velocity_val=1.5,
    )
    project = Project.load(saved["project_path"])
    assert project.metadata.name == "Round trip"
    assert project.flow.mach() == pytest.approx(1.5)


def test_saving_a_project_with_bad_values_is_refused(store, tmp_path):
    """Model bounds apply to projects too."""
    response = call(
        server_module.save_project,
        name="Bad",
        path=str(tmp_path / "bad.atsproj"),
        aoa_deg=75.0,
    )
    assert response["ok"] is False
    assert "invalid parameters" in response["error"]


def test_loading_a_missing_project_is_reported(tmp_path):
    """A wrong path gives a structured error, not an exception."""
    response = call(server_module.load_project, path=str(tmp_path / "no.atsproj"))
    assert response["ok"] is False
    assert "not found" in response["error"]


def test_projects_can_be_listed(store, tmp_path):
    """An agent can discover what studies already exist."""
    call(
        server_module.save_project,
        name="Listed", path=str(tmp_path / "listed.atsproj")
    )
    response = call(server_module.list_saved_projects, directory=str(tmp_path))
    assert response["ok"]
    assert response["count"] == 1
    assert response["projects"][0]["name"] == "Listed"


def test_settings_can_be_read_and_updated(store):
    """Preferences are reachable over MCP, with bounds enforced."""
    before = call(server_module.get_settings)
    assert before["ok"]
    assert "default_mpi_ranks" in before

    updated = call(
        server_module.update_settings, default_mpi_ranks=6, default_colormap="viridis"
    )
    assert updated["ok"]
    assert updated["default_mpi_ranks"] == 6
    assert updated["default_colormap"] == "viridis"
    assert updated["changed"] == {
        "default_mpi_ranks": 6,
        "default_colormap": "viridis",
    }

    rejected = call(server_module.update_settings, default_mpi_ranks=9999)
    assert rejected["ok"] is False


def test_updating_settings_leaves_other_fields_alone(store):
    """Only the supplied fields change."""
    call(server_module.update_settings, default_colormap="plasma")
    before = call(server_module.get_settings)
    call(server_module.update_settings, default_mpi_ranks=4)
    after = call(server_module.get_settings)
    assert after["default_colormap"] == before["default_colormap"] == "plasma"
    assert after["default_mpi_ranks"] == 4
