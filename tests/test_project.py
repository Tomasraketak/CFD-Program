"""Project files and persistent application settings.

A project is the document an operator's work lives in, so the emphasis here is
on the properties that make it trustworthy: a full round trip loses nothing,
a corrupted file is reported rather than half-read, and a file from a future
version is refused instead of silently misinterpreted.
"""

from __future__ import annotations

import json

import pytest

from core import PROJECT_FORMAT_VERSION
from core.models import (
    AxisDirection,
    DomainShape,
    GeometryParams,
    HingeAxis,
    MeshResolution,
    ThermalParams,
    VelocityType,
)
from core.project import (
    PROJECT_EXTENSION,
    Project,
    ProjectError,
    ProjectMetadata,
    SweepSettings,
    default_project,
    list_projects,
    projects_directory,
)
from core.settings import (
    MAX_RECENT_PROJECTS,
    AppSettings,
    load_settings,
    reset_settings_cache,
    save_settings,
)


def populated_project() -> Project:
    """A project with every section filled in."""
    project = default_project("Fin sizing study")
    project.metadata.description = "Hinge torque across the Mach range"
    project.metadata.author = "Test"
    project.notes = "Fin thickness 4 mm; check servo at Mach 3."
    project.geometry = GeometryParams(
        step_file_path="/models/rocket.step",
        nose_direction=AxisDirection.MINUS_Z,
        reference_origin=[0.01, 0.0, 0.0],
        scale_to_meters=0.001,
    )
    project.domain.upstream_multiplier = 7.5
    project.domain.shape = DomainShape.BOX
    project.mesh.resolution = MeshResolution.FINE
    project.mesh.boundary_layers = 8
    project.flow.velocity_value = 2.75
    project.flow.aoa_deg = -6.5
    project.solver.mpi_ranks = 12
    project.hinge_axes = [
        HingeAxis(name="fin_a", point=[0.9, 0.05, 0.0], direction=[0.0, 1.0, 0.0]),
        HingeAxis(name="fin_b", point=[0.9, 0.0, 0.05], direction=[0.0, 0.0, 1.0]),
    ]
    project.sweep = SweepSettings(parameter="aoa", values=[-10.0, 0.0, 10.0])
    project.last_mesh_id = "mesh-20260101-000000-abcdef"
    return project


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_save_and_load_preserves_everything(tmp_path):
    """Nothing is lost between saving and reopening a project."""
    original = populated_project()
    path = original.save(tmp_path / "study")
    loaded = Project.load(path)

    assert loaded.metadata.name == "Fin sizing study"
    assert loaded.metadata.author == "Test"
    assert loaded.notes == original.notes
    assert loaded.geometry is not None
    assert loaded.geometry.step_file_path == "/models/rocket.step"
    assert loaded.geometry.nose_direction is AxisDirection.MINUS_Z
    assert loaded.geometry.scale_to_meters == pytest.approx(0.001)
    assert loaded.domain.shape is DomainShape.BOX
    assert loaded.domain.upstream_multiplier == pytest.approx(7.5)
    assert loaded.mesh.resolution is MeshResolution.FINE
    assert loaded.mesh.boundary_layers == 8
    assert loaded.flow.aoa_deg == pytest.approx(-6.5)
    assert loaded.flow.mach() == pytest.approx(2.75)
    assert loaded.solver.mpi_ranks == 12
    assert [axis.name for axis in loaded.hinge_axes] == ["fin_a", "fin_b"]
    assert loaded.sweep.parameter == "aoa"
    assert loaded.sweep.values == [-10.0, 0.0, 10.0]
    assert loaded.last_mesh_id == original.last_mesh_id
    assert loaded.thermal is not None


def test_extension_is_added_when_missing(tmp_path):
    """Saving without an extension still produces a .atsproj file."""
    path = default_project().save(tmp_path / "unnamed")
    assert path.suffix == PROJECT_EXTENSION
    assert path.is_file()


def test_saved_file_is_readable_json(tmp_path):
    """Projects are meant to be diffable and inspectable by hand."""
    path = populated_project().save(tmp_path / "study")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["metadata"]["name"] == "Fin sizing study"
    assert payload["flow"]["aoa_deg"] == pytest.approx(-6.5)


def test_saving_refreshes_the_modification_time(tmp_path):
    """The stored timestamp reflects the last save, not creation."""
    project = default_project()
    original = project.metadata.created_at
    project.metadata.modified_at = "2000-01-01T00:00:00+00:00"
    project.save(tmp_path / "p")
    assert project.metadata.modified_at != "2000-01-01T00:00:00+00:00"
    assert project.metadata.created_at == original


def test_save_is_atomic_and_leaves_no_temporary(tmp_path):
    """An interrupted save must not leave a half-written project behind."""
    path = populated_project().save(tmp_path / "study")
    assert path.is_file()
    assert list(tmp_path.glob("*.tmp")) == []


def test_defaults_alone_make_a_valid_project(tmp_path):
    """A brand-new project saves and reloads without any configuration."""
    path = default_project().save(tmp_path / "fresh")
    loaded = Project.load(path)
    assert loaded.geometry is None
    assert loaded.thermal is not None  # the reference tram scenario


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_missing_file_is_reported(tmp_path):
    """A path that does not exist gives a clear message."""
    with pytest.raises(ProjectError, match="not found"):
        Project.load(tmp_path / "absent.atsproj")


def test_corrupted_json_is_reported(tmp_path):
    """A damaged file is named, not silently ignored."""
    path = tmp_path / "broken.atsproj"
    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(ProjectError, match="not a valid project file"):
        Project.load(path)


def test_wrong_shape_payload_is_reported(tmp_path):
    """JSON that is not an object is rejected."""
    path = tmp_path / "list.atsproj"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ProjectError, match="does not contain a project"):
        Project.load(path)


def test_future_format_version_is_refused(tmp_path):
    """A newer file must not be partially read.

    Silently ignoring fields an older build does not understand would lose
    settings without telling anyone, so the load is refused outright.
    """
    project = default_project()
    path = project.save(tmp_path / "future")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["metadata"]["format_version"] = PROJECT_FORMAT_VERSION + 5
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProjectError, match="format v"):
        Project.load(path)


def test_invalid_values_are_reported_with_their_location(tmp_path):
    """A file with an out-of-range value names the offending field."""
    path = tmp_path / "bad.atsproj"
    payload = json.loads(default_project().save(tmp_path / "seed").read_text())
    payload["flow"]["aoa_deg"] = 95.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProjectError, match="aoa_deg"):
        Project.load(path)


# ---------------------------------------------------------------------------
# Derived requests
# ---------------------------------------------------------------------------


def test_project_builds_a_mesh_request():
    """A project drives the meshing pipeline directly."""
    project = populated_project()
    request = project.to_mesh_request()
    assert request.geometry.step_file_path == "/models/rocket.step"
    assert request.domain.shape is DomainShape.BOX
    assert request.sizing_flow is not None
    assert request.sizing_flow.mach() == pytest.approx(2.75)


def test_mesh_request_without_geometry_is_refused():
    """Meshing needs CAD, and says so rather than failing deep inside Gmsh."""
    with pytest.raises(ProjectError, match="no CAD file"):
        default_project().to_mesh_request()


def test_project_builds_an_aero_request():
    """A project drives a solve, carrying its hinges through."""
    request = populated_project().to_aero_request("mesh-7")
    assert request.mesh_id == "mesh-7"
    assert len(request.hinge_axes) == 2
    assert request.solver.mpi_ranks == 12


def test_summary_describes_the_project():
    """The summary is what listings and the recent menu show."""
    summary = populated_project().summary()
    assert summary["name"] == "Fin sizing study"
    assert summary["hinge_count"] == 2
    assert summary["sweep_points"] == 3
    assert summary["has_geometry"] is True
    assert summary["mach"] == pytest.approx(2.75)


# ---------------------------------------------------------------------------
# Sweep settings
# ---------------------------------------------------------------------------


def test_sweep_values_parse_from_text():
    """The GUI edits sweeps as free text, which must parse forgivingly."""
    sweep = SweepSettings.from_text("mach", "0.5, 1.0 , 2.0;3.0")
    assert sweep.values == [0.5, 1.0, 2.0, 3.0]
    assert sweep.parameter == "mach"


def test_sweep_text_round_trips():
    """Values survive a trip through the text representation."""
    original = SweepSettings(parameter="aoa", values=[-5.0, 0.0, 5.0])
    reparsed = SweepSettings.from_text("aoa", original.values_text())
    assert reparsed.values == original.values


def test_sweep_rejects_non_numeric_entries():
    """A typo names the offending token instead of failing obscurely."""
    with pytest.raises(ProjectError, match="'abc' is not a number"):
        SweepSettings.from_text("mach", "1.0, abc, 2.0")


def test_empty_sweep_text_yields_no_values():
    """Clearing the field is allowed; the sweep simply has nothing to run."""
    assert SweepSettings.from_text("mach", "   ").values == []


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_list_projects_summarises_a_directory(tmp_path):
    """Listing shows each project with its summary, newest first."""
    populated_project().save(tmp_path / "one")
    default_project("Second").save(tmp_path / "two")

    entries = list_projects(tmp_path)
    assert len(entries) == 2
    assert all(entry["readable"] for entry in entries)
    assert {entry["name"] for entry in entries} == {"Fin sizing study", "Second"}


def test_list_projects_reports_unreadable_files(tmp_path):
    """A corrupted project stays visible rather than silently disappearing."""
    populated_project().save(tmp_path / "good")
    (tmp_path / "bad.atsproj").write_text("{broken", encoding="utf-8")

    entries = list_projects(tmp_path)
    assert len(entries) == 2
    broken = [entry for entry in entries if not entry["readable"]]
    assert len(broken) == 1
    assert broken[0]["error"]


def test_list_projects_of_an_empty_directory(tmp_path):
    """No projects is not an error."""
    assert list_projects(tmp_path) == []


def test_projects_directory_is_created(isolated_data_root):
    """The default projects folder exists as soon as it is asked for."""
    directory = projects_directory()
    assert directory.is_dir()
    assert directory.name == "projects"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_round_trip(isolated_data_root):
    """Preferences survive a save and reload."""
    settings = AppSettings()
    settings.default_mpi_ranks = 10
    settings.default_colormap = "viridis"
    settings.confirm_on_exit = False
    settings.save()

    reloaded = AppSettings.load()
    assert reloaded.default_mpi_ranks == 10
    assert reloaded.default_colormap == "viridis"
    assert reloaded.confirm_on_exit is False


def test_settings_default_when_absent(isolated_data_root):
    """A machine with no settings file gets sensible defaults."""
    settings = AppSettings.load()
    assert settings.default_mpi_ranks >= 1
    assert settings.recent_projects == []


def test_corrupted_settings_fall_back_to_defaults(isolated_data_root):
    """A damaged settings file must never stop the application starting.

    Losing a remembered window size is trivial; being locked out is not.
    """
    path = AppSettings.path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json at all", encoding="utf-8")

    settings = AppSettings.load()
    assert settings.default_mpi_ranks >= 1


def test_settings_reject_out_of_range_values():
    """Bounds are enforced, so a bad value cannot reach the solver."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AppSettings(default_mpi_ranks=0)
    with pytest.raises(ValidationError):
        AppSettings(default_mpi_ranks=500)


def test_recent_projects_move_to_the_front(isolated_data_root, tmp_path):
    """Reopening a project promotes it, without duplicating the entry."""
    settings = AppSettings()
    first = tmp_path / "a.atsproj"
    second = tmp_path / "b.atsproj"
    for path in (first, second):
        default_project().save(path)

    settings.remember_project(first)
    settings.remember_project(second)
    settings.remember_project(first)

    assert len(settings.recent_projects) == 2
    assert settings.recent_projects[0] == str(first.resolve())


def test_recent_projects_are_capped(isolated_data_root, tmp_path):
    """The menu stays a usable length."""
    settings = AppSettings()
    for index in range(MAX_RECENT_PROJECTS + 8):
        settings.remember_project(tmp_path / f"p{index}.atsproj")
    assert len(settings.recent_projects) == MAX_RECENT_PROJECTS


def test_only_existing_recent_projects_are_offered(isolated_data_root, tmp_path):
    """A deleted project is not offered, but is kept in the stored list.

    The drive may simply not be mounted right now, so forgetting it outright
    would be unhelpful.
    """
    settings = AppSettings()
    present = tmp_path / "present.atsproj"
    default_project().save(present)
    settings.remember_project(present)
    settings.remember_project(tmp_path / "gone.atsproj")

    assert len(settings.recent_projects) == 2
    assert settings.existing_recent_projects() == [str(present.resolve())]


def test_forget_project_removes_an_entry(isolated_data_root, tmp_path):
    """An unreadable project can be dropped from the list."""
    settings = AppSettings()
    path = tmp_path / "x.atsproj"
    settings.remember_project(path)
    settings.forget_project(path)
    assert settings.recent_projects == []


def test_remembering_a_project_records_its_directory(isolated_data_root, tmp_path):
    """The next Open dialog starts where the last project lives."""
    settings = AppSettings()
    settings.remember_project(tmp_path / "study.atsproj")
    assert settings.last_project_directory == str(tmp_path)


def test_cached_settings_are_shared(isolated_data_root):
    """load_settings returns one instance, so edits are not lost."""
    reset_settings_cache()
    first = load_settings()
    first.default_colormap = "plasma"
    save_settings(first)
    assert load_settings().default_colormap == "plasma"
