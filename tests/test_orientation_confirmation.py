"""The operator sees how the air meets the model before anything is paid for.

A rocket meshed tail first returns a complete set of plausible forces, and
the operator's own session did exactly that: the assistant passed
nose_direction '+Y' for a rocket whose nose is at +Y, and meshed it
backwards. The built-in assistant now has to draw the setup and wait for
the operator's reply before it may mesh or solve it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mcp")

import mcp_server as server  # noqa: E402
from backend.ai_agent import AIAssistant, FakeChatClient  # noqa: E402
from backend.runner import FakeRunner  # noqa: E402
from tests.test_mcp_server import (  # noqa: E402
    FORCES_BREAKDOWN,
    capture_geometry,
    loaded_rocket,
    screen_rows,
)


@pytest.fixture(autouse=True)
def gated(monkeypatch):
    """Turn the gate on, draw nothing, and leave no approvals behind."""
    server.set_runner(
        FakeRunner(lines=screen_rows(), artifacts={"forces_breakdown.dat": FORCES_BREAKDOWN})
    )
    server.reset_orientation_confirmations()
    server.require_orientation_confirmation(True)

    def fake_render(geometry, output_path, aoa_deg=0.0, sideslip_deg=0.0, caption="", pivot_height_m=0.0):
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        return path

    import backend.orientation_preview as preview

    monkeypatch.setattr(preview, "render_orientation_preview", fake_render)
    yield
    server.require_orientation_confirmation(False)
    server.reset_orientation_confirmations()
    server.set_runner(None)


@pytest.fixture
def mesh_id(store) -> str:
    record = store.create("mesh", {"step_file_path": "/models/rocket.step"})
    record.path("mesh.su2").write_text("NDIME= 3\nNELEM= 0\nNPOIN= 0\nNMARK= 0\n")
    store.update_metadata(
        record.record_id,
        {
            "mesh_path": str(record.path("mesh.su2")),
            "reference_area_m2": 0.005,
            "reference_length_m": 0.08,
            "geometry_key": ["/models/rocket.step", [1.0, 0.0, 0.0], None, "rocket"],
        },
    )
    return record.record_id


def preview_mesh(mesh_id, monkeypatch, store, **angles):
    """Preview an existing mesh; its request file is faked for the purpose."""
    from core.models import GeometryParams, MeshRequest

    store.write_json(
        mesh_id,
        "mesh_request.json",
        MeshRequest(geometry=GeometryParams(step_file_path="/models/rocket.step")),
    )
    return server.preview_orientation(mesh_id=mesh_id, **angles)


def test_an_unseen_setup_is_not_solved(mesh_id):
    reply = server.run_aerodynamic_simulation(mesh_id=mesh_id, velocity_val=0.3, aoa_deg=5.0)
    assert reply["ok"] is False
    assert reply["needs"] == ["operator_confirmation"]
    assert "preview_orientation" in reply["error"]


def test_showing_the_picture_is_not_enough_the_operator_must_answer(mesh_id, monkeypatch, store):
    shown = preview_mesh(mesh_id, monkeypatch, store, aoa_deg=5.0)
    assert shown["ok"] and Path(shown["image_path"]).is_file()

    early = server.run_aerodynamic_simulation(mesh_id=mesh_id, velocity_val=0.3, aoa_deg=5.0)
    assert early["ok"] is False

    server.acknowledge_orientation_previews()
    answered = server.run_aerodynamic_simulation(mesh_id=mesh_id, velocity_val=0.3, aoa_deg=5.0)
    assert answered["ok"] is True, answered


def test_a_different_angle_is_a_new_setup(mesh_id, monkeypatch, store):
    preview_mesh(mesh_id, monkeypatch, store, aoa_deg=0.0)
    server.acknowledge_orientation_previews()
    assert server.run_aerodynamic_simulation(mesh_id=mesh_id, velocity_val=0.3)["ok"]
    assert not server.run_aerodynamic_simulation(
        mesh_id=mesh_id, velocity_val=0.3, aoa_deg=5.0
    )["ok"]


def test_meshing_waits_for_the_approved_geometry(tmp_path, monkeypatch):
    seen = capture_geometry(monkeypatch)
    step = loaded_rocket(tmp_path)

    refused = server.set_geometry_and_mesh(step_file_path=step, nose_direction="-X")
    assert refused["needs"] == ["operator_confirmation"]
    assert "step_file_path" not in seen

    # A preview of the other end is not an approval of this one.
    server.preview_orientation(step_file_path=step, nose_direction="+X")
    server.acknowledge_orientation_previews()
    assert not server.set_geometry_and_mesh(step_file_path=step, nose_direction="-X")["ok"]
    assert "step_file_path" not in seen

    server.preview_orientation(step_file_path=step, nose_direction="-X")
    server.acknowledge_orientation_previews()
    server.set_geometry_and_mesh(step_file_path=step, nose_direction="-X")
    assert seen["nose"].value == "-X"


def test_external_clients_are_not_gated(mesh_id):
    server.require_orientation_confirmation(False)
    assert server.run_aerodynamic_simulation(mesh_id=mesh_id, velocity_val=0.3)["ok"]


def test_the_preview_says_where_the_nose_is(tmp_path):
    reply = server.preview_orientation(step_file_path=loaded_rocket(tmp_path), nose_direction="-X")
    assert reply["nose_at"] == "the +X end of the CAD model"


def test_an_explicit_plus_x_is_obeyed(tmp_path, monkeypatch):
    """'+X' used to be taken for 'not given', so it could never be chosen."""
    server.require_orientation_confirmation(False)
    seen = capture_geometry(monkeypatch)
    server.set_geometry_and_mesh(step_file_path=loaded_rocket(tmp_path), nose_direction="+X")
    assert seen["nose"].value == "+X"


# ---------------------------------------------------------------------------
# The assistant stops and waits
# ---------------------------------------------------------------------------


def _tool(name, arguments, call_id):
    import json

    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def test_the_assistant_ends_its_turn_after_a_preview(mesh_id, store):
    from core.models import GeometryParams, MeshRequest

    store.write_json(
        mesh_id, "mesh_request.json",
        MeshRequest(geometry=GeometryParams(step_file_path="/models/rocket.step")),
    )
    client = FakeChatClient(
        [
            _tool("preview_orientation", {"mesh_id": mesh_id, "aoa_deg": 5.0}, "p1"),
            # It tries to press on in the same breath; that must not run.
            _tool(
                "run_aerodynamic_simulation",
                {"mesh_id": mesh_id, "velocity_val": 0.3, "aoa_deg": 5.0},
                "r1",
            ),
        ]
    )
    assistant = AIAssistant(client, confirm_orientation=True)
    reply = assistant.ask("Simulate at 5 degrees")

    assert [call.name for call in reply.tool_calls] == ["preview_orientation"]
    assert "Reply to confirm" in reply.text
    assert assistant.messages[-1]["role"] == "assistant"

    # The operator's reply is the approval.
    client.responses.extend(
        [
            _tool(
                "run_aerodynamic_simulation",
                {"mesh_id": mesh_id, "velocity_val": 0.3, "aoa_deg": 5.0},
                "r2",
            ),
            {"role": "assistant", "content": "Done."},
        ]
    )
    second = assistant.ask("Yes, that is right")
    assert second.tool_calls[0].name == "run_aerodynamic_simulation"
    assert second.tool_calls[0].succeeded, second.tool_calls[0].result
    assert second.text == "Done."


def test_overlay_moves_the_tilt_axis_with_the_pivot():
    """The orange rod sits where the pivot is, up along rocket +X."""
    pytest.importorskip("pyvista")
    from gui.flow_overlay import build_overlay, describe

    bounds = (-0.05, 0.05, -0.05, 0.05, -1.0, 0.0)
    level = build_overlay(bounds)
    raised = build_overlay(bounds, pivot_height_m=0.2)
    rod_level = [m for m, o in level if o.get("color") == "#f0a030"][0]
    rod_raised = [m for m, o in raised if o.get("color") == "#f0a030"][0]
    assert rod_raised.center[0] - rod_level.center[0] == pytest.approx(0.2, abs=1e-6)
    assert "0.2 m above" in describe(0.0, 0.0, None, 0.2)
    assert "below" in describe(0.0, 0.0, None, -0.1)
