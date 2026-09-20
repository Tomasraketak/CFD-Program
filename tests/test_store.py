"""Run-store behaviour shared by the GUI and the MCP server."""

from __future__ import annotations

import pytest

from core.models import FlowParams
from core.platform_env import probe_environment, recommended_mpi_ranks
from core.store import RecordNotFoundError, RunStore, default_store


def test_create_and_get_round_trip(store: RunStore):
    """A created record can be read back with its metadata intact."""
    record = store.create("mesh", {"cell_count": 421_000})
    loaded = store.get(record.record_id)
    assert loaded.kind == "mesh"
    assert loaded.metadata["cell_count"] == 421_000
    assert loaded.directory.is_dir()


def test_ids_are_unique_and_prefixed_by_kind(store: RunStore):
    """Identifiers are readable and collision-free."""
    ids = {store.create("aero").record_id for _ in range(25)}
    assert len(ids) == 25
    assert all(identifier.startswith("aero-") for identifier in ids)


def test_missing_record_raises(store: RunStore):
    """Unknown identifiers raise a typed error the MCP layer can report."""
    with pytest.raises(RecordNotFoundError):
        store.get("mesh-does-not-exist")


def test_update_metadata_merges_rather_than_replaces(store: RunStore):
    """Updating adds keys while preserving existing ones."""
    record = store.create("thermal", {"a": 1})
    store.update_metadata(record.record_id, {"b": 2})
    updated = store.update_metadata(record.record_id, {"a": 99})
    assert updated.metadata == {"a": 99, "b": 2}
    assert store.get(record.record_id).metadata == {"a": 99, "b": 2}


def test_pydantic_models_are_storable(store: RunStore):
    """Parameter models serialise into a record and read back faithfully."""
    record = store.create("aero")
    flow = FlowParams(velocity_value=2.5, aoa_deg=-3.0)
    store.write_json(record.record_id, "flow.json", flow)
    payload = store.read_json(record.record_id, "flow.json")
    assert FlowParams(**payload).mach() == pytest.approx(2.5)
    assert payload["aoa_deg"] == -3.0


def test_reading_a_missing_file_raises(store: RunStore):
    """A record without the requested file reports it clearly."""
    record = store.create("aero")
    with pytest.raises(RecordNotFoundError, match="no file"):
        store.read_json(record.record_id, "absent.json")


def test_list_records_filters_by_kind_and_sorts_newest_first(store: RunStore):
    """Listing is filterable and ordered for GUI presentation."""
    meshes = [store.create("mesh") for _ in range(3)]
    store.create("aero")
    listed = store.list_records("mesh")
    assert {r.record_id for r in listed} == {m.record_id for m in meshes}
    assert [r.created_at for r in listed] == sorted(
        (r.created_at for r in listed), reverse=True
    )
    assert len(store.list_records()) == 4


def test_resolve_mesh_path_requires_a_real_mesh_file(store: RunStore):
    """A mesh record only resolves once the .su2 file exists on disk."""
    record = store.create("mesh")
    with pytest.raises(RecordNotFoundError, match="no mesh file"):
        store.resolve_mesh_path(record.record_id)
    record.path("mesh.su2").write_text("NDIME= 3\n", encoding="utf-8")
    assert store.resolve_mesh_path(record.record_id).name == "mesh.su2"


def test_resolve_mesh_path_rejects_non_mesh_records(store: RunStore):
    """Passing a simulation id where a mesh id belongs is caught."""
    record = store.create("aero")
    with pytest.raises(RecordNotFoundError, match="not a mesh"):
        store.resolve_mesh_path(record.record_id)


def test_delete_removes_the_directory(store: RunStore):
    """Deleting a record removes it and its payload from disk."""
    record = store.create("mesh")
    directory = record.directory
    store.delete(record.record_id)
    assert not directory.exists()
    with pytest.raises(RecordNotFoundError):
        store.get(record.record_id)


def test_default_store_honours_the_isolated_data_root(isolated_data_root):
    """The process-wide store respects ATS_DATA_ROOT so tests stay hermetic."""
    assert default_store().root == isolated_data_root.resolve()


def test_stray_directories_are_ignored(store: RunStore):
    """A directory without metadata does not break listing."""
    (store.runs_dir / "not-a-record").mkdir()
    store.create("mesh")
    assert len(store.list_records()) == 1


def test_environment_probe_reports_missing_toolchain():
    """The probe degrades gracefully where SU2/MPI are absent."""
    report = probe_environment(probe_versions=False)
    assert report.cpu_count >= 1
    assert report.recommended_ranks == recommended_mpi_ranks()
    assert isinstance(report.solver_ready, bool)
    if not report.solver_ready:
        assert report.missing
    assert set(report.as_dict()) >= {"os_name", "data_root", "solver_ready", "missing"}
