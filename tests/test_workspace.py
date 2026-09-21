"""The hand-off between the interface and the assistant.

The feature these tests protect is small and easy to break: an operator
imports a STEP file in the GUI and then asks the assistant to mesh "the
model I just imported". Without this record the assistant has to ask for a
path, which is exactly the tedium the program exists to remove.
"""

from __future__ import annotations

import json

import pytest

from core.workspace import (
    ActiveGeometry,
    active_geometry,
    clear_active_geometry,
    set_active_geometry,
    workspace_path,
)
from tests.test_step_inspect import MILLIMETRES, write_step


@pytest.fixture
def rocket(tmp_path):
    """A millimetre-scale rocket 1.32 m long, nose at the +X end."""
    from tests.test_step_inspect import rocket_points

    return write_step(tmp_path / "Sapphire.step", MILLIMETRES, rocket_points())


@pytest.fixture
def tube(tmp_path):
    """A body with two equally thick ends: no nose to find."""
    from tests.test_step_inspect import rocket_points

    return write_step(
        tmp_path / "tube.step", MILLIMETRES, rocket_points(nose_radius=90.0)
    )


def test_nothing_is_loaded_to_begin_with():
    """A fresh installation has no geometry, and says so without complaint."""
    assert active_geometry() is None


def test_a_chosen_file_survives_to_the_next_reader(rocket):
    """What the interface records is what a tool call reads back."""
    set_active_geometry(rocket)
    loaded = active_geometry()
    assert loaded is not None
    assert loaded.step_file_path == rocket
    assert loaded.exists()


def test_the_unit_is_read_from_the_file(rocket):
    """The operator should not have to know their exporter's conventions."""
    record = set_active_geometry(rocket)
    assert record.scale_to_meters == pytest.approx(1e-3)
    assert record.scale_is_confident
    assert record.largest_extent_m == pytest.approx(1.32)
    assert "millimetres" in record.summary()


def test_an_explicit_scale_is_not_second_guessed(rocket):
    """A project's saved unit is a decision, not a suggestion."""
    record = set_active_geometry(rocket, scale_to_meters=1.0)
    assert record.scale_to_meters == pytest.approx(1.0)
    assert record.scale_is_confident
    assert record.largest_extent_m == pytest.approx(1320.0)


def test_the_nose_direction_travels_with_the_file(rocket):
    """The assistant should not have to re-ask which way the rocket points."""
    set_active_geometry(rocket, nose_direction="+Y")
    assert active_geometry().nose_direction == "+Y"


def test_the_nose_is_read_off_the_shape(rocket):
    """Importing a rocket should settle which way it points."""
    record = set_active_geometry(rocket)
    assert record.nose_end == "+X"
    # Meshing rotates nose-to-tail onto +X, so the parameter is the opposite.
    assert record.nose_direction == "-X"
    assert record.nose_is_confident
    assert record.open_questions() == []
    assert "nose at +X" in record.summary()


def test_a_body_with_no_obvious_nose_becomes_a_question(tube):
    """Not knowing is reported, not defaulted.

    A wrong nose direction meshes the rocket backwards and returns a full
    set of plausible forces for a vehicle flying tail-first.
    """
    record = set_active_geometry(tube)
    assert not record.nose_is_confident
    assert record.nose_end is None
    questions = record.open_questions()
    assert len(questions) == 1
    assert "which end" in questions[0].lower()
    assert "X" in questions[0]


def test_an_explicit_nose_direction_is_kept_but_disagreement_is_noted(rocket):
    """The operator decides, and is told when the shape says otherwise."""
    record = set_active_geometry(rocket, nose_direction="+X")
    assert record.nose_direction == "+X"
    assert record.nose_is_confident
    assert "the shape suggests -X" in record.nose_reason


def test_slenderness_is_recorded(rocket):
    """Used to decide whether the nose question applies at all."""
    assert set_active_geometry(rocket).slenderness > 2.0


def test_choosing_another_file_replaces_the_first(rocket, tmp_path):
    """This is a pointer to the bench, not a history."""
    second = write_step(tmp_path / "other.step", MILLIMETRES, [(0, 0, 0), (500, 50, 50)])
    set_active_geometry(rocket)
    set_active_geometry(second)
    assert active_geometry().step_file_path == second


def test_clearing_forgets_the_file(rocket):
    """Emptying the path box means nothing is loaded."""
    set_active_geometry(rocket)
    clear_active_geometry()
    assert active_geometry() is None


def test_clearing_nothing_is_not_an_error():
    """Called on a fresh installation, this must be a no-op."""
    clear_active_geometry()
    clear_active_geometry()


def test_a_damaged_record_reads_as_nothing_loaded(isolated_data_root):
    """Losing this costs one retyped path; it must never raise."""
    target = workspace_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json at all", encoding="utf-8")
    assert active_geometry() is None


def test_an_unknown_field_reads_as_nothing_loaded(isolated_data_root):
    """A record from a future version is not silently half-applied."""
    target = workspace_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"step_file_path": "x.step", "invented_field": 1}),
        encoding="utf-8",
    )
    assert active_geometry() is None


def test_a_file_that_has_since_been_deleted_is_reported_as_missing(rocket, tmp_path):
    """The path is recorded; whether it still resolves is asked, not assumed."""
    set_active_geometry(rocket)
    (tmp_path / "Sapphire.step").unlink()
    loaded = active_geometry()
    assert loaded is not None
    assert not loaded.exists()
    assert loaded.as_dict()["file_exists"] is False


def test_a_path_that_does_not_exist_is_still_recorded(tmp_path):
    """Someone typing a path should see their own text, not have it dropped."""
    record = set_active_geometry(tmp_path / "not-there-yet.step")
    assert record.step_file_path.endswith("not-there-yet.step")
    assert not record.exists()
    assert record.scale_to_meters == pytest.approx(1.0)


def test_the_record_serialises_for_a_tool_reply(rocket):
    """The MCP tool returns this dictionary; it must be JSON and complete."""
    set_active_geometry(rocket, nose_direction="+Y")
    payload = active_geometry().as_dict()
    json.dumps(payload)
    assert payload["units"] == "millimetres"
    assert payload["nose_direction"] == "+Y"
    assert payload["file_exists"] is True
    assert payload["scale_to_meters"] == pytest.approx(1e-3)


def test_the_source_records_which_surface_chose_the_file(rocket):
    """When the GUI and an agent disagree, the log should say who last set it."""
    set_active_geometry(rocket, source="mcp")
    assert active_geometry().source == "mcp"


def test_a_summary_reads_as_a_sentence(rocket):
    """This goes in the status bar, so it has to be legible."""
    record = set_active_geometry(rocket)
    assert record.summary() == (
        "Sapphire.step, read as millimetres — 1.32 m long, nose at +X"
    )


def test_an_empty_record_summarises_without_a_file():
    """Defensive: a record with no path must not raise on display."""
    record = ActiveGeometry(step_file_path="")
    assert "(none)" in record.summary()
