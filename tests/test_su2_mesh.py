"""SU2 mesh assembly and serialisation."""

from __future__ import annotations

import numpy as np
import pytest

from backend.su2_mesh import (
    VTK_TETRA,
    VTK_TRIANGLE,
    VTK_WEDGE,
    ElementBlock,
    MeshAssemblyError,
    SU2Mesh,
    merge_nodes,
    read_su2_header,
    triangles_to_marker,
)

TETRA_POINTS = np.array(
    [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
)


def single_tetra_mesh() -> SU2Mesh:
    """A one-cell mesh with every face tagged."""
    mesh = SU2Mesh(points=TETRA_POINTS)
    mesh.add_volume(VTK_TETRA, np.array([[0, 1, 2, 3]]))
    mesh.add_marker("WALL", VTK_TRIANGLE, np.array([[0, 1, 2]]))
    mesh.add_marker(
        "FARFIELD", VTK_TRIANGLE, np.array([[0, 1, 3], [0, 2, 3], [1, 2, 3]])
    )
    return mesh


def test_element_block_rejects_wrong_node_count():
    """A block's array width must match its element type."""
    with pytest.raises(MeshAssemblyError, match="needs 4 nodes"):
        ElementBlock(VTK_TETRA, np.array([[0, 1, 2]]))


def test_element_block_rejects_unknown_type():
    """Unsupported element types are caught at construction."""
    with pytest.raises(MeshAssemblyError, match="unsupported element type"):
        ElementBlock(999, np.array([[0, 1, 2]]))


def test_cell_and_marker_counts():
    """Counts reported for the mesh report are correct."""
    mesh = single_tetra_mesh()
    assert mesh.cell_count == 1
    assert mesh.node_count == 4
    assert mesh.marker_counts() == {"WALL": 1, "FARFIELD": 3}
    assert mesh.cell_counts_by_type() == {"tetrahedra": 1}


def test_mixed_element_counts():
    """Prisms and tetrahedra are counted separately."""
    points = np.vstack([TETRA_POINTS, TETRA_POINTS + np.array([0.0, 0.0, 2.0])])
    mesh = SU2Mesh(points=points)
    mesh.add_volume(VTK_TETRA, np.array([[0, 1, 2, 3]]))
    mesh.add_volume(VTK_WEDGE, np.array([[0, 1, 2, 4, 5, 6]]))
    assert mesh.cell_counts_by_type() == {"tetrahedra": 1, "prisms": 1}
    assert mesh.cell_count == 2


def test_volume_and_surface_types_are_not_interchangeable():
    """A triangle cannot be a cell and a tetrahedron cannot be a boundary."""
    mesh = SU2Mesh(points=TETRA_POINTS)
    with pytest.raises(MeshAssemblyError, match="not a volume element"):
        mesh.add_volume(VTK_TRIANGLE, np.array([[0, 1, 2]]))
    with pytest.raises(MeshAssemblyError, match="is a volume element"):
        mesh.add_marker("WALL", VTK_TETRA, np.array([[0, 1, 2, 3]]))


def test_empty_blocks_are_ignored():
    """Adding zero elements does not create an empty block or marker."""
    mesh = SU2Mesh(points=TETRA_POINTS)
    mesh.add_marker("WALL", VTK_TRIANGLE, np.empty((0, 3), dtype=int))
    assert mesh.markers == {}


def test_validate_rejects_out_of_range_indices():
    """An element referencing a missing node is caught before writing."""
    mesh = SU2Mesh(points=TETRA_POINTS)
    mesh.add_volume(VTK_TETRA, np.array([[0, 1, 2, 99]]))
    with pytest.raises(MeshAssemblyError, match="outside the 4 available nodes"):
        mesh.validate()


def test_validate_rejects_a_mesh_without_cells():
    """SU2 needs at least one volume cell."""
    with pytest.raises(MeshAssemblyError, match="no volume cells"):
        SU2Mesh(points=TETRA_POINTS).validate()


def test_validate_rejects_non_finite_coordinates():
    """NaN coordinates would silently corrupt the solve."""
    points = TETRA_POINTS.copy()
    points[0, 0] = np.nan
    mesh = SU2Mesh(points=points)
    mesh.add_volume(VTK_TETRA, np.array([[0, 1, 2, 3]]))
    with pytest.raises(MeshAssemblyError, match="non-finite"):
        mesh.validate()


def test_points_must_be_three_dimensional():
    """A 2-column point array is rejected at construction."""
    with pytest.raises(MeshAssemblyError, match=r"\(N, 3\)"):
        SU2Mesh(points=np.zeros((4, 2)))


def test_written_file_has_the_expected_structure(tmp_path):
    """The file follows the SU2 native layout with VTK type codes."""
    path = single_tetra_mesh().write(tmp_path / "mesh.su2")
    lines = path.read_text().splitlines()

    assert lines[0] == "NDIME= 3"
    assert lines[1] == "NELEM= 1"
    # Element line: VTK type 10 (tetra), four nodes, then the running index.
    assert lines[2].split() == ["10", "0", "1", "2", "3", "0"]
    assert "NPOIN= 4" in lines
    assert "NMARK= 2" in lines
    assert "MARKER_TAG= WALL" in lines
    assert "MARKER_ELEMS= 3" in lines


def test_written_coordinates_are_plain_numbers(tmp_path):
    """NumPy scalars must not leak into the file as 'np.float64(...)'.

    SU2's parser would reject that, and the failure would only appear when the
    solver runs.
    """
    path = single_tetra_mesh().write(tmp_path / "mesh.su2")
    text = path.read_text()
    assert "np.float64" not in text
    start = text.index("NPOIN=")
    coordinate_line = text[start:].splitlines()[1]
    values = coordinate_line.split()
    assert len(values) == 4
    for value in values[:3]:
        float(value)  # raises if not a plain number


def test_coordinates_round_trip_exactly(tmp_path):
    """Full double precision survives serialisation."""
    points = np.array([[0.1234567890123456, -1.0e-7, 3.999999999999999]])
    mesh = SU2Mesh(points=np.vstack([points, TETRA_POINTS]))
    mesh.add_volume(VTK_TETRA, np.array([[0, 1, 2, 3]]))
    path = mesh.write(tmp_path / "mesh.su2")

    text = path.read_text()
    first = text[text.index("NPOIN=") :].splitlines()[1].split()
    assert float(first[0]) == points[0, 0]
    assert float(first[2]) == points[0, 2]


def test_read_header_matches_what_was_written(tmp_path):
    """The header reader recovers the counts and marker names."""
    path = single_tetra_mesh().write(tmp_path / "mesh.su2")
    header = read_su2_header(path)
    assert header["dimension"] == 3
    assert header["cell_count"] == 1
    assert header["node_count"] == 4
    assert header["markers"] == {"WALL": 1, "FARFIELD": 3}


def test_write_creates_missing_directories(tmp_path):
    """Writing into a fresh run directory works without pre-creating it."""
    path = single_tetra_mesh().write(tmp_path / "runs" / "a" / "mesh.su2")
    assert path.is_file()


# ---------------------------------------------------------------------------
# Node merging, used to stitch prisms onto tetrahedra
# ---------------------------------------------------------------------------


def test_merge_nodes_fuses_shared_points():
    """Coincident nodes in two blocks collapse to one merged node."""
    first = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    second = np.array([[1.0, 0.0, 0.0], [2.0, 0.0, 0.0]])
    merged, (map_a, map_b) = merge_nodes([first, second])

    assert len(merged) == 3
    assert map_a[1] == map_b[0]
    assert np.allclose(merged[map_a], first)
    assert np.allclose(merged[map_b], second)


def test_merge_nodes_preserves_every_input_coordinate():
    """Each original point maps to a merged node at the same location."""
    rng = np.random.default_rng(0)
    first = rng.normal(size=(50, 3))
    second = np.vstack([first[:10], rng.normal(size=(20, 3))])
    merged, (map_a, map_b) = merge_nodes([first, second])
    assert np.allclose(merged[map_a], first, atol=1e-9)
    assert np.allclose(merged[map_b], second, atol=1e-9)
    assert len(merged) == 70


def test_merge_nodes_handles_no_blocks():
    """Merging nothing yields an empty array rather than raising."""
    merged, mappings = merge_nodes([])
    assert merged.shape == (0, 3)
    assert mappings == []


def test_triangles_to_marker_remaps_indices():
    """Marker connectivity follows the merged node numbering."""
    triangles = np.array([[0, 1, 2]])
    mapping = np.array([5, 6, 7])
    assert triangles_to_marker(triangles, mapping).tolist() == [[5, 6, 7]]
    assert triangles_to_marker(triangles).tolist() == [[0, 1, 2]]
