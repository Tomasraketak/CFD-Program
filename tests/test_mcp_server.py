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
    assert set(response["forces_n"]) == {"fx", "fy", "fz"}
    assert len(response["center_of_pressure"]) == 3
    assert response["hinge_torques"][0]["name"] == "fin_1"
    assert math.isfinite(response["hinge_torques"][0]["torque_nm"])
    assert response["sim_id"]


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
