"""Prism boundary-layer extrusion.

The extruder exists because Gmsh cannot build 3D prism layers, so these tests
carry the burden of proving it works on progressively harder geometry:
a smooth sphere, a cylinder with sharp rims (the case Gmsh's own extruder
fails on), a thin plate with opposing walls, a concave torus, and a cone whose
apex Gmsh meshes non-manifold.
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.prism_layers import (
    MIN_VISIBILITY,
    PrismExtrusionError,
    build_adjacency,
    compute_vertex_normals,
    diagnose_surface,
    extrude_prism_layers,
    layers_for_thickness,
    smooth_directions,
    stack_height,
    suggested_max_thickness,
    surface_signed_volume,
    triangle_normals_and_areas,
)
from tests.geometry_helpers import (
    icosphere,
    surface_mesh_of,
    unit_cube_surface,
)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def test_triangle_normals_and_areas_on_a_known_triangle():
    """A right triangle in the xy-plane has area 0.5 and a +z normal."""
    points = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    triangles = np.array([[0, 1, 2]])
    normals, areas = triangle_normals_and_areas(points, triangles)
    assert areas[0] == pytest.approx(0.5)
    assert normals[0] == pytest.approx([0.0, 0.0, 1.0])


def test_degenerate_triangle_yields_zero_not_nan():
    """Collinear points produce a zero normal and area rather than NaN."""
    points = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]])
    normals, areas = triangle_normals_and_areas(points, np.array([[0, 1, 2]]))
    assert areas[0] == 0.0
    assert np.all(np.isfinite(normals))
    assert normals[0] == pytest.approx([0.0, 0.0, 0.0])


def test_signed_volume_of_a_unit_cube():
    """The divergence-theorem volume matches the analytic cube volume."""
    points, triangles = unit_cube_surface()
    assert abs(surface_signed_volume(points, triangles)) == pytest.approx(1.0)


def test_signed_volume_sign_follows_winding():
    """Reversing the winding flips the sign of the enclosed volume."""
    points, triangles = unit_cube_surface()
    forward = surface_signed_volume(points, triangles)
    reversed_winding = surface_signed_volume(points, triangles[:, ::-1])
    assert forward == pytest.approx(-reversed_winding)


def test_vertex_normals_point_outward_regardless_of_winding():
    """Orientation comes from the enclosed volume, not the caller's winding."""
    points, triangles = icosphere(subdivisions=2)
    for winding in (triangles, triangles[:, ::-1]):
        normals = compute_vertex_normals(points, winding, outward=True)
        radial = points / np.linalg.norm(points, axis=1)[:, None]
        # On a sphere the outward normal is the radial direction.
        assert np.all(np.einsum("ij,ij->i", normals, radial) > 0.9)


def test_vertex_normals_can_point_inward():
    """Inward marching is available for internal ducts."""
    points, triangles = icosphere(subdivisions=2)
    normals = compute_vertex_normals(points, triangles, outward=False)
    radial = points / np.linalg.norm(points, axis=1)[:, None]
    assert np.all(np.einsum("ij,ij->i", normals, radial) < -0.9)


def test_vertex_normals_are_unit_length():
    """Every returned normal is normalised."""
    points, triangles = icosphere(subdivisions=2)
    normals = compute_vertex_normals(points, triangles)
    assert np.allclose(np.linalg.norm(normals, axis=1), 1.0)


def test_smoothing_cannot_rotate_a_direction_far_from_its_normal():
    """The spherical clamp is what keeps slender tips from inverting.

    Without it, symmetric cancellation of radial normals near a tip drags the
    marching direction onto the body axis and shears the prisms.
    """
    points, triangles = icosphere(subdivisions=2)
    normals = compute_vertex_normals(points, triangles)
    smoothed = smooth_directions(
        normals,
        build_adjacency(triangles, len(points)),
        passes=25,
        max_deviation_deg=10.0,
    )
    alignment = np.einsum("ij,ij->i", smoothed, normals)
    assert np.all(alignment >= np.cos(np.radians(10.0)) - 1e-9)
    assert np.allclose(np.linalg.norm(smoothed, axis=1), 1.0)


def test_smoothing_with_zero_passes_is_a_no_op():
    """Requesting no smoothing returns the input directions untouched."""
    points, triangles = icosphere(subdivisions=1)
    normals = compute_vertex_normals(points, triangles)
    adjacency = build_adjacency(triangles, len(points))
    assert np.allclose(smooth_directions(normals, adjacency, passes=0), normals)


# ---------------------------------------------------------------------------
# Surface diagnostics
# ---------------------------------------------------------------------------


def test_diagnose_clean_surface():
    """A closed manifold surface reports no defects."""
    points, triangles = icosphere(subdivisions=2)
    diagnostic = diagnose_surface(points, triangles)
    assert diagnostic.is_closed
    assert diagnostic.is_manifold
    assert diagnostic.boundary_edges == 0
    assert diagnostic.non_manifold_edges == 0
    assert "closed manifold" in diagnostic.describe()


def test_diagnose_detects_an_open_surface():
    """Removing a triangle opens the surface and is reported."""
    points, triangles = icosphere(subdivisions=1)
    diagnostic = diagnose_surface(points, triangles[1:])
    assert diagnostic.boundary_edges == 3
    assert not diagnostic.is_closed
    assert "boundary" in diagnostic.describe()


def test_diagnose_detects_orphan_and_duplicate_vertices():
    """Unused and coincident vertices are counted."""
    points, triangles = icosphere(subdivisions=1)
    padded = np.vstack([points, points[0], [[9.0, 9.0, 9.0]]])
    diagnostic = diagnose_surface(padded, triangles)
    assert diagnostic.orphan_vertices == 2
    assert diagnostic.duplicate_vertices >= 1


def test_diagnostic_is_json_serialisable():
    """The report embeds into MCP responses and the mesh record."""
    points, triangles = icosphere(subdivisions=1)
    payload = diagnose_surface(points, triangles).as_dict()
    assert payload["is_manifold"] is True
    assert isinstance(payload["triangle_count"], int)


# ---------------------------------------------------------------------------
# Extrusion on smooth geometry
# ---------------------------------------------------------------------------


def test_sphere_extrusion_matches_the_analytic_offset():
    """On a sphere every vertex marches the full stack height radially."""
    points, triangles = icosphere(subdivisions=3, radius=1.0)
    first_height, layers, growth = 1.0e-3, 7, 1.25
    result = extrude_prism_layers(points, triangles, first_height, layers, growth)

    assert result.layers_built == layers
    assert result.frozen_vertices.size == 0
    assert result.min_wedge_volume > 0.0
    assert result.wedge_count == layers * len(triangles)

    expected = 1.0 + stack_height(first_height, layers, growth)
    outer_radii = np.linalg.norm(result.points[result.outer_triangles.ravel()], axis=1)
    # The visibility limiter trims slightly on a faceted sphere, so the
    # achieved offset approaches but never exceeds the analytic one.
    assert np.all(outer_radii <= expected + 1e-12)
    assert outer_radii.min() == pytest.approx(expected, rel=0.05)


def test_all_wedges_have_positive_volume():
    """No inverted cells: SU2 rejects a mesh containing any."""
    points, triangles = icosphere(subdivisions=3)
    result = extrude_prism_layers(points, triangles, 1.0e-3, 6, 1.3)
    from backend.prism_layers import _wedge_volumes

    assert np.all(_wedge_volumes(result.points, result.wedges) > 0.0)


def test_layer_count_and_node_blocks_are_consistent():
    """Node array holds one block per layer plus the original wall."""
    points, triangles = icosphere(subdivisions=2)
    result = extrude_prism_layers(points, triangles, 1.0e-3, 5, 1.2)
    assert result.points.shape[0] == (result.layers_built + 1) * len(points)
    assert np.array_equal(result.wall_triangles, triangles)
    # The outer shell indexes the final node block.
    assert result.outer_triangles.min() >= result.layers_built * len(points)


def test_inward_extrusion_shrinks_the_surface():
    """Marching inward offsets towards the centre, for internal ducts."""
    points, triangles = icosphere(subdivisions=2, radius=1.0)
    result = extrude_prism_layers(
        points, triangles, 1.0e-3, 5, 1.2, outward=False
    )
    outer_radii = np.linalg.norm(result.points[result.outer_triangles.ravel()], axis=1)
    assert np.all(outer_radii < 1.0)
    assert result.min_wedge_volume > 0.0


def test_max_thickness_caps_the_stack():
    """An explicit thickness cap is respected at every vertex."""
    points, triangles = icosphere(subdivisions=2)
    cap = 2.0e-3
    result = extrude_prism_layers(
        points, triangles, 1.0e-3, 8, 1.4, max_thickness=cap
    )
    assert np.all(result.vertex_thickness <= cap + 1e-12)


# ---------------------------------------------------------------------------
# Extrusion on hard geometry, via Gmsh
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,builder,size",
    [
        ("cylinder", "cylinder", 0.02),
        ("thin_plate", "plate", 0.004),
        ("torus", "torus", 0.02),
    ],
)
def test_sharp_and_concave_geometry_extrudes_cleanly(name, builder, size):
    """Geometry that defeats Gmsh's own extruder produces a valid stack.

    The cylinder is the specific case where ``geo.extrudeBoundaryLayer``
    reports overlapping facets.
    """
    points, triangles = surface_mesh_of(builder, size)
    diagnostic = diagnose_surface(points, triangles)
    assert diagnostic.is_manifold, f"{name}: {diagnostic.describe()}"

    result = extrude_prism_layers(points, triangles, 6.5e-5, 7, 1.25)
    assert result.layers_built == 7
    assert result.min_wedge_volume > 0.0
    assert result.frozen_vertices.size == 0


def test_thin_plate_thins_the_stack_between_opposing_walls():
    """The proximity limiter stops opposing faces marching through each other."""
    points, triangles = surface_mesh_of("plate", 0.004)
    result = extrude_prism_layers(points, triangles, 6.5e-5, 7, 1.25)
    unlimited = stack_height(6.5e-5, 7, 1.25)
    # The plate is 4 mm thick, so the stack must be trimmed somewhere.
    assert result.vertex_thickness.min() < unlimited
    assert result.min_wedge_volume > 0.0


def test_non_manifold_surface_is_refused_with_an_actionable_message():
    """A sharp cone apex makes Gmsh emit non-manifold edges; say so plainly."""
    points, triangles = surface_mesh_of("cone", 0.012)
    diagnostic = diagnose_surface(points, triangles)
    assert diagnostic.non_manifold_edges > 0

    with pytest.raises(PrismExtrusionError, match="not a closed manifold"):
        extrude_prism_layers(points, triangles, 6.5e-5, 7, 1.25)


def test_defect_tolerance_freezes_only_the_affected_region():
    """Tolerating defects degrades locally, not globally."""
    points, triangles = surface_mesh_of("cone", 0.012)
    result = extrude_prism_layers(
        points, triangles, 6.5e-5, 7, 1.25, tolerate_defects=True
    )
    assert result.layers_built == 7
    assert result.min_wedge_volume > 0.0
    # Only a handful of vertices at the apex are sacrificed.
    assert result.frozen_vertices.size < 0.01 * len(points)
    assert any("non-manifold" in message for message in result.warnings)


# ---------------------------------------------------------------------------
# Input validation and helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"layers": 0}, "layer count"),
        ({"first_height": 0.0}, "first layer height"),
        ({"growth_rate": 0.5}, "growth rate"),
    ],
)
def test_invalid_parameters_are_rejected(kwargs, match):
    """Nonsensical stack parameters raise rather than producing junk."""
    points, triangles = icosphere(subdivisions=1)
    arguments = {
        "first_height": 1.0e-3,
        "layers": 5,
        "growth_rate": 1.2,
        **kwargs,
    }
    with pytest.raises(PrismExtrusionError, match=match):
        extrude_prism_layers(points, triangles, **arguments)


def test_empty_triangulation_is_rejected():
    """An empty wall cannot be extruded."""
    with pytest.raises(PrismExtrusionError, match="no wall triangles"):
        extrude_prism_layers(
            np.zeros((3, 3)), np.empty((0, 3), dtype=int), 1e-3, 3, 1.2
        )


def test_malformed_arrays_are_rejected():
    """Wrong-shaped inputs are caught at the boundary."""
    with pytest.raises(PrismExtrusionError, match=r"\(N, 3\)"):
        extrude_prism_layers(np.zeros((4, 2)), np.array([[0, 1, 2]]), 1e-3, 3, 1.2)


def test_stack_height_matches_the_geometric_series():
    """Total stack height follows the closed-form sum."""
    assert stack_height(1.0, 4, 2.0) == pytest.approx(15.0)
    assert stack_height(0.5, 6, 1.0) == pytest.approx(3.0)


def test_layers_for_thickness_inverts_stack_height():
    """The layer count that fits a thickness is consistent with the sum."""
    first, growth = 1.0e-4, 1.25
    for layers in (3, 5, 8):
        thickness = stack_height(first, layers, growth)
        assert layers_for_thickness(first, growth, thickness) == layers
    assert layers_for_thickness(1.0e-4, 1.25, 1.0e-5) == 0


def test_suggested_max_thickness_tracks_the_smallest_dimension():
    """The suggested cap is a fraction of the thinnest extent."""
    points = np.array([[0.0, 0.0, 0.0], [10.0, 2.0, 0.5]])
    assert suggested_max_thickness(points, np.empty((0, 3)), 0.25) == pytest.approx(
        0.125
    )


def test_report_is_json_serialisable():
    """The summary embeds directly into the mesh report."""
    points, triangles = icosphere(subdivisions=2)
    report = extrude_prism_layers(points, triangles, 1e-3, 5, 1.2).report()
    assert report["layers_built"] == 5
    assert report["min_wedge_volume"] > 0.0
    assert set(report) >= {"wedge_count", "frozen_vertex_count", "mean_thickness_m"}


def test_visibility_floor_is_respected():
    """The visibility limiter never scales a step to zero on its own."""
    assert 0.0 < MIN_VISIBILITY < 1.0
