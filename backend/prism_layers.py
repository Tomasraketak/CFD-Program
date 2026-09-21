"""Advancing-layer prism boundary-layer extrusion.

Gmsh cannot generate 3D prism boundary layers: its ``BoundaryLayer`` field is
2D-only (it exposes curve and point lists, not surfaces), and
``geo.extrudeBoundaryLayer`` produces overlapping facets on anything with a
sharp edge -- including a plain closed cylinder. Anisotropic near-wall cells
are not optional for this platform, though: holding y+ ~= 45 on a 1 m body
with isotropic tetrahedra would take millions of surface triangles alone,
several times the 750k cell ceiling that keeps a solve inside 3-8 minutes.

This module therefore implements the extrusion directly. It takes a closed
triangulated wall surface, marches a stack of layers outward along smoothed
vertex normals, and emits wedge (prism) elements plus the outer shell
triangles that the remaining farfield volume is tet-meshed against.

The hard part of advancing-layer extrusion is self-intersection where the
surface is concave or where two walls approach each other -- fin roots and the
nose tip on a rocket. Three independent limiters constrain each vertex's step,
and any vertex whose stack would still collide is frozen early, yielding a
locally thinner but geometrically valid stack rather than tangled cells.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.spatial import cKDTree

# A vertex whose normal deviates strongly from its incident face normals sits
# on a sharp convex ridge or in a concave valley; its step is scaled by the
# cosine, floored here so a step never collapses entirely.
MIN_VISIBILITY = 0.15

# A step is never allowed to exceed this fraction of the distance to the
# nearest surface point that is not a one-ring neighbour. This is what stops
# opposing walls (a thin fin, a narrow gap) from marching through each other.
PROXIMITY_SAFETY = 0.40

# Number of Laplacian smoothing passes applied to the marching directions.
DEFAULT_SMOOTHING_PASSES = 4

# Smoothing may never rotate a marching direction further than this from the
# vertex's own surface normal. Without the clamp, symmetric cancellation at a
# slender tip drags the direction onto the body axis and inverts the prisms.
MAX_SMOOTHING_DEVIATION_DEG = 30.0

# Neighbours examined per vertex in the proximity query.
_PROXIMITY_NEIGHBOURS = 16

# Local step-repair attempts per layer before an offending vertex is frozen.
_MAX_REPAIR_ATTEMPTS = 8


@dataclass
class PrismLayerResult:
    """Output of a boundary-layer extrusion.

    Attributes
    ----------
    points:
        ``(P, 3)`` array of all node coordinates: the original wall nodes
        followed by one block of nodes per layer.
    wedges:
        ``(W, 6)`` array of prism connectivity indexing into ``points``. Node
        ordering is the VTK/SU2 convention: bottom triangle then top triangle.
    outer_triangles:
        ``(T, 3)`` triangulation of the outermost surface of the stack, to be
        handed to the volume mesher as the inner boundary of the tet region.
    wall_triangles:
        ``(T, 3)`` triangulation of the original wall, unchanged.
    layer_heights:
        ``(L,)`` nominal (unlimited) height of each layer.
    vertex_thickness:
        ``(N,)`` total thickness actually achieved at each wall vertex.
    frozen_vertices:
        Indices of vertices whose march was stopped early by a limiter.
    min_wedge_volume:
        Smallest wedge volume produced; must be strictly positive.
    layers_requested, layers_built:
        Requested and achieved layer counts.
    """

    points: np.ndarray
    wedges: np.ndarray
    outer_triangles: np.ndarray
    wall_triangles: np.ndarray
    layer_heights: np.ndarray
    vertex_thickness: np.ndarray
    frozen_vertices: np.ndarray
    min_wedge_volume: float
    layers_requested: int
    layers_built: int
    warnings: list[str] = field(default_factory=list)
    diagnostic: "SurfaceDiagnostic | None" = None

    @property
    def wall_node_count(self) -> int:
        """Number of nodes on the original wall surface."""
        return int(self.wall_triangles.max()) + 1 if self.wall_triangles.size else 0

    @property
    def wedge_count(self) -> int:
        """Number of prism elements generated."""
        return int(self.wedges.shape[0])

    def achieved_first_height(self) -> float:
        """Median height of the first layer after limiting."""
        if self.vertex_thickness.size == 0:
            return 0.0
        return float(np.median(self.layer_heights[:1]))

    def report(self) -> dict[str, float | int]:
        """Summary suitable for the mesh report and MCP responses."""
        return {
            "wedge_count": self.wedge_count,
            "layers_requested": self.layers_requested,
            "layers_built": self.layers_built,
            "frozen_vertex_count": int(self.frozen_vertices.size),
            "min_wedge_volume": self.min_wedge_volume,
            "mean_thickness_m": float(np.mean(self.vertex_thickness))
            if self.vertex_thickness.size
            else 0.0,
            "min_thickness_m": float(np.min(self.vertex_thickness))
            if self.vertex_thickness.size
            else 0.0,
            "max_thickness_m": float(np.max(self.vertex_thickness))
            if self.vertex_thickness.size
            else 0.0,
        }


class PrismExtrusionError(RuntimeError):
    """Raised when a valid prism stack cannot be produced."""


@dataclass
class SurfaceDiagnostic:
    """Topological health of a wall triangulation.

    A prism march assumes a closed, manifold surface. Real surface meshes
    violate that in specific, recognisable ways -- a mathematically sharp cone
    apex, for instance, makes Gmsh emit edges shared by four triangles -- and
    the failure surfaces much later as inverted cells unless it is caught here.
    """

    vertex_count: int
    triangle_count: int
    boundary_edges: int
    non_manifold_edges: int
    degenerate_triangles: int
    orphan_vertices: int
    duplicate_vertices: int
    signed_volume: float
    non_manifold_vertices: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=np.int64)
    )

    @property
    def is_closed(self) -> bool:
        """True when every edge is shared by at least two triangles."""
        return self.boundary_edges == 0

    @property
    def is_manifold(self) -> bool:
        """True when every edge is shared by exactly two triangles."""
        return self.boundary_edges == 0 and self.non_manifold_edges == 0

    def describe(self) -> str:
        """Human-readable summary for error messages and mesh reports."""
        problems = []
        if self.boundary_edges:
            problems.append(f"{self.boundary_edges} boundary (open) edges")
        if self.non_manifold_edges:
            problems.append(f"{self.non_manifold_edges} non-manifold edges")
        if self.degenerate_triangles:
            problems.append(f"{self.degenerate_triangles} degenerate triangles")
        if self.duplicate_vertices:
            problems.append(f"{self.duplicate_vertices} duplicate vertices")
        if not problems:
            return (
                f"closed manifold surface: {self.vertex_count} vertices, "
                f"{self.triangle_count} triangles"
            )
        return (
            f"{self.vertex_count} vertices, {self.triangle_count} triangles; "
            + ", ".join(problems)
        )

    def as_dict(self) -> dict[str, int | float | bool]:
        """JSON-serialisable form for the mesh report."""
        return {
            "vertex_count": self.vertex_count,
            "triangle_count": self.triangle_count,
            "boundary_edges": self.boundary_edges,
            "non_manifold_edges": self.non_manifold_edges,
            "degenerate_triangles": self.degenerate_triangles,
            "orphan_vertices": self.orphan_vertices,
            "duplicate_vertices": self.duplicate_vertices,
            "signed_volume": self.signed_volume,
            "is_closed": self.is_closed,
            "is_manifold": self.is_manifold,
        }


def diagnose_surface(
    points: np.ndarray,
    triangles: np.ndarray,
    degenerate_area_ratio: float = 1.0e-8,
    duplicate_tolerance: float | None = None,
) -> SurfaceDiagnostic:
    """Inspect a wall triangulation for the defects that break extrusion.

    Parameters
    ----------
    points:
        ``(N, 3)`` vertex coordinates.
    triangles:
        ``(T, 3)`` vertex indices.
    degenerate_area_ratio:
        Triangles with area below this multiple of the median area are
        counted as degenerate.
    duplicate_tolerance:
        Distance below which two vertices count as coincident. Defaults to
        1e-10 of the bounding-box diagonal.

    Returns
    -------
    SurfaceDiagnostic
        Counts of each defect class plus the vertices on non-manifold edges.
    """
    points = np.asarray(points, dtype=float)
    triangles = np.asarray(triangles, dtype=np.int64)

    edges = np.concatenate(
        [triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]], axis=0
    )
    edges = np.sort(edges, axis=1)
    unique_edges, counts = np.unique(edges, axis=0, return_counts=True)

    boundary_edges = int(np.count_nonzero(counts == 1))
    non_manifold_mask = counts > 2
    non_manifold_edges = int(np.count_nonzero(non_manifold_mask))
    non_manifold_vertices = (
        np.unique(unique_edges[non_manifold_mask]) if non_manifold_edges else
        np.empty(0, dtype=np.int64)
    )

    _, areas = triangle_normals_and_areas(points, triangles)
    median_area = float(np.median(areas)) if areas.size else 0.0
    degenerate = int(np.count_nonzero(areas <= degenerate_area_ratio * median_area))

    referenced = np.unique(triangles)
    orphans = int(points.shape[0] - referenced.size)

    if duplicate_tolerance is None:
        diagonal = float(
            np.linalg.norm(points.max(axis=0) - points.min(axis=0))
        ) if points.size else 0.0
        duplicate_tolerance = max(diagonal, 1.0) * 1.0e-10
    duplicates = (
        len(cKDTree(points).query_pairs(duplicate_tolerance)) if points.size else 0
    )

    return SurfaceDiagnostic(
        vertex_count=int(points.shape[0]),
        triangle_count=int(triangles.shape[0]),
        boundary_edges=boundary_edges,
        non_manifold_edges=non_manifold_edges,
        degenerate_triangles=degenerate,
        orphan_vertices=orphans,
        duplicate_vertices=int(duplicates),
        signed_volume=surface_signed_volume(points, triangles),
        non_manifold_vertices=non_manifold_vertices,
    )


def triangle_normals_and_areas(
    points: np.ndarray, triangles: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Unit normals and areas of every triangle.

    Parameters
    ----------
    points:
        ``(N, 3)`` vertex coordinates.
    triangles:
        ``(T, 3)`` vertex indices.

    Returns
    -------
    tuple of ndarray
        ``(T, 3)`` unit normals and ``(T,)`` areas. Degenerate triangles get a
        zero normal and zero area rather than NaN.
    """
    v0 = points[triangles[:, 0]]
    v1 = points[triangles[:, 1]]
    v2 = points[triangles[:, 2]]
    cross = np.cross(v1 - v0, v2 - v0)
    twice_area = np.linalg.norm(cross, axis=1)
    areas = 0.5 * twice_area
    normals = np.zeros_like(cross)
    valid = twice_area > 0.0
    normals[valid] = cross[valid] / twice_area[valid, None]
    return normals, areas


def surface_signed_volume(points: np.ndarray, triangles: np.ndarray) -> float:
    """Signed volume enclosed by a closed triangulated surface.

    Positive when the triangle winding yields outward-pointing normals, via
    the divergence theorem. Used to orient the march without trusting the
    caller's winding convention.
    """
    v0 = points[triangles[:, 0]]
    v1 = points[triangles[:, 1]]
    v2 = points[triangles[:, 2]]
    return float(np.sum(np.einsum("ij,ij->i", v0, np.cross(v1, v2))) / 6.0)


def compute_vertex_normals(
    points: np.ndarray, triangles: np.ndarray, outward: bool = True
) -> np.ndarray:
    """Area-weighted vertex normals, oriented consistently.

    Parameters
    ----------
    points:
        ``(N, 3)`` vertex coordinates.
    triangles:
        ``(T, 3)`` vertex indices of a closed surface.
    outward:
        When True the returned normals point out of the enclosed solid, i.e.
        into the surrounding fluid, which is the direction a body's boundary
        layer grows.

    Returns
    -------
    ndarray
        ``(N, 3)`` unit vertex normals.
    """
    face_normals, areas = triangle_normals_and_areas(points, triangles)
    weighted = face_normals * areas[:, None]

    normals = np.zeros_like(points)
    for corner in range(3):
        np.add.at(normals, triangles[:, corner], weighted)

    # Resolve orientation from the enclosed volume rather than trusting the
    # caller's winding, and apply it up front so the singular-vertex fallback
    # below can be written in unambiguous geometric terms.
    flip = outward != (surface_signed_volume(points, triangles) > 0.0)
    if flip:
        normals = -normals

    # A cone apex, or any rotationally symmetric tip, has incident face
    # normals that cancel exactly, leaving a zero-length sum. Such a vertex
    # has no usable averaged normal, and leaving it at zero collapses its
    # prisms to zero volume. Fall back to the direction from the one-ring
    # centroid to the vertex, which points correctly out of a tip.
    scale = float(np.linalg.norm(points.max(axis=0) - points.min(axis=0)))
    tolerance = max(scale, 1.0) * 1.0e-12
    lengths = np.linalg.norm(normals, axis=1)
    degenerate = np.flatnonzero(lengths <= tolerance)
    if degenerate.size:
        ring_sum = np.zeros_like(points)
        ring_count = np.zeros(len(points))
        for corner in range(3):
            indices = triangles[:, corner]
            for other in range(3):
                if other == corner:
                    continue
                np.add.at(ring_sum, indices, points[triangles[:, other]])
                np.add.at(ring_count, indices, 1.0)
        ring_count[ring_count == 0.0] = 1.0
        centroids = ring_sum / ring_count[:, None]
        # Points away from the body at a tip, which is the outward march
        # direction; negate it when marching inward.
        fallback = points[degenerate] - centroids[degenerate]
        if not outward:
            fallback = -fallback
        fallback_lengths = np.linalg.norm(fallback, axis=1)
        usable = fallback_lengths > tolerance
        fallback[usable] /= fallback_lengths[usable, None]
        # An isolated vertex with no usable geometry gets an arbitrary but
        # finite direction so the march stays well defined.
        fallback[~usable] = np.array([0.0, 0.0, 1.0])
        normals[degenerate] = fallback
        lengths = np.linalg.norm(normals, axis=1)

    lengths[lengths <= 0.0] = 1.0
    return normals / lengths[:, None]


def build_adjacency(triangles: np.ndarray, vertex_count: int) -> list[set[int]]:
    """One-ring vertex adjacency, including each vertex itself."""
    adjacency: list[set[int]] = [set() for _ in range(vertex_count)]
    for a, b, c in triangles:
        adjacency[a].update((a, b, c))
        adjacency[b].update((a, b, c))
        adjacency[c].update((a, b, c))
    return adjacency


def smooth_directions(
    directions: np.ndarray,
    adjacency: list[set[int]],
    passes: int = DEFAULT_SMOOTHING_PASSES,
    relaxation: float = 0.5,
    max_deviation_deg: float = MAX_SMOOTHING_DEVIATION_DEG,
) -> np.ndarray:
    """Laplacian-smooth marching directions and renormalise.

    Smoothing keeps the offset field continuous across creases such as a fin
    root, where raw vertex normals change abruptly and would otherwise fold
    the stack over itself.

    Unconstrained smoothing is actively harmful near a slender tip, though:
    the surface normals there are mostly radial and cancel by symmetry when
    averaged, so a few passes rotate the marching direction onto the body
    axis. Neighbouring vertices then shear past one another and the prisms
    inverted. Each smoothed direction is therefore spherically clamped to stay
    within ``max_deviation_deg`` of its own surface normal.
    """
    max_deviation = math.radians(max(0.0, min(90.0, max_deviation_deg)))
    cos_max = math.cos(max_deviation)
    sin_max = math.sin(max_deviation)

    smoothed = directions.copy()
    for _ in range(max(0, passes)):
        averaged = np.zeros_like(smoothed)
        for index, neighbours in enumerate(adjacency):
            others = [n for n in neighbours if n != index]
            if others:
                averaged[index] = smoothed[list(others)].mean(axis=0)
            else:
                averaged[index] = smoothed[index]
        blended = (1.0 - relaxation) * smoothed + relaxation * averaged
        lengths = np.linalg.norm(blended, axis=1)
        lengths[lengths <= 0.0] = 1.0
        candidate = blended / lengths[:, None]

        # Spherically clamp back towards the original normal where the smooth
        # has strayed too far.
        alignment = np.einsum("ij,ij->i", candidate, directions)
        strayed = alignment < cos_max
        if np.any(strayed):
            base = directions[strayed]
            perpendicular = (
                candidate[strayed] - alignment[strayed, None] * base
            )
            perpendicular_length = np.linalg.norm(perpendicular, axis=1)
            usable = perpendicular_length > 1.0e-14
            clamped = base.copy()
            clamped[usable] = (
                cos_max * base[usable]
                + sin_max
                * perpendicular[usable]
                / perpendicular_length[usable, None]
            )
            candidate[strayed] = clamped
        smoothed = candidate
    return smoothed


def _visibility_factors(
    directions: np.ndarray, triangles: np.ndarray, face_normals: np.ndarray
) -> np.ndarray:
    """Per-vertex step scaling from agreement with incident face normals.

    A vertex in a concave valley has a marching direction that leans away from
    its faces; stepping the full height there is what drives facets through
    one another. The minimum cosine over the incident faces is a cheap, robust
    proxy for how much room the vertex actually has.
    """
    vertex_count = directions.shape[0]
    cos_min = np.ones(vertex_count)
    for corner in range(3):
        indices = triangles[:, corner]
        cosines = np.einsum("ij,ij->i", directions[indices], face_normals)
        np.minimum.at(cos_min, indices, cosines)
    return np.clip(cos_min, MIN_VISIBILITY, 1.0)


def _proximity_limits(
    points: np.ndarray, adjacency: list[set[int]]
) -> np.ndarray:
    """Maximum safe step per vertex from the nearest non-neighbour surface point.

    Uses a KD-tree over the current front. Neighbours in the one-ring are
    excluded because they are legitimately close; anything else that close is
    an opposing wall.
    """
    vertex_count = points.shape[0]
    tree = cKDTree(points)
    neighbour_count = min(_PROXIMITY_NEIGHBOURS, vertex_count)
    distances, indices = tree.query(points, k=neighbour_count)

    limits = np.full(vertex_count, np.inf)
    for vertex in range(vertex_count):
        ring = adjacency[vertex]
        for distance, other in zip(distances[vertex], indices[vertex]):
            if other == vertex or other in ring:
                continue
            if distance > 0.0:
                limits[vertex] = distance
            break
    return limits * PROXIMITY_SAFETY


def _wedge_volumes(points: np.ndarray, wedges: np.ndarray) -> np.ndarray:
    """Volume of each prism, decomposed into three tetrahedra."""
    p = [points[wedges[:, i]] for i in range(6)]
    tets = ((0, 1, 2, 3), (1, 2, 3, 4), (2, 3, 4, 5))
    volume = np.zeros(wedges.shape[0])
    for a, b, c, d in tets:
        volume += np.einsum(
            "ij,ij->i", p[b] - p[a], np.cross(p[c] - p[a], p[d] - p[a])
        ) / 6.0
    return volume


def extrude_prism_layers(
    points: np.ndarray,
    triangles: np.ndarray,
    first_height: float,
    layers: int,
    growth_rate: float,
    outward: bool = True,
    smoothing_passes: int = DEFAULT_SMOOTHING_PASSES,
    max_thickness: float | None = None,
    tolerate_defects: bool = False,
) -> PrismLayerResult:
    """March a prism boundary layer outward from a closed wall surface.

    Parameters
    ----------
    points:
        ``(N, 3)`` wall vertex coordinates.
    triangles:
        ``(T, 3)`` wall triangulation, assumed closed and manifold.
    first_height:
        Height of the first layer, typically from
        :func:`core.units.first_cell_height` for the target y+.
    layers:
        Number of layers to march (5-8 for this platform).
    growth_rate:
        Geometric ratio between successive layer heights.
    outward:
        March away from the enclosed solid (the usual case for a body in a
        flow). Set False to march inward, as for an internal duct.
    smoothing_passes:
        Laplacian passes applied to the marching directions.
    max_thickness:
        Optional hard cap on total stack thickness, metres.
    tolerate_defects:
        When True, a surface that is not a closed manifold is accepted and the
        vertices touching the offending edges are frozen, producing a locally
        thinner but valid stack instead of raising.

    Returns
    -------
    PrismLayerResult
        Node coordinates, prism connectivity and the outer shell.

    Raises
    ------
    PrismExtrusionError
        If the inputs are degenerate or no layer could be built with positive
        volume everywhere.
    """
    points = np.ascontiguousarray(points, dtype=float)
    triangles = np.ascontiguousarray(triangles, dtype=np.int64)

    if points.ndim != 2 or points.shape[1] != 3:
        raise PrismExtrusionError("points must be an (N, 3) array")
    if triangles.ndim != 2 or triangles.shape[1] != 3:
        raise PrismExtrusionError("triangles must be a (T, 3) array")
    if triangles.size == 0:
        raise PrismExtrusionError("no wall triangles to extrude from")
    if layers < 1:
        raise PrismExtrusionError(f"layer count must be >= 1, got {layers}")
    if first_height <= 0.0:
        raise PrismExtrusionError(
            f"first layer height must be positive, got {first_height}"
        )
    if growth_rate < 1.0:
        raise PrismExtrusionError(
            f"growth rate must be >= 1.0, got {growth_rate}"
        )

    vertex_count = points.shape[0]
    adjacency = build_adjacency(triangles, vertex_count)

    diagnostic = diagnose_surface(points, triangles)
    if not diagnostic.is_manifold and not tolerate_defects:
        raise PrismExtrusionError(
            "wall surface is not a closed manifold, which a prism march "
            f"requires ({diagnostic.describe()}). Remesh the surface -- a "
            "mathematically sharp tip such as a cone apex is the usual cause "
            "-- or pass tolerate_defects=True to freeze the affected vertices "
            "and march a locally thinner stack."
        )

    normals = compute_vertex_normals(points, triangles, outward=outward)
    directions = smooth_directions(normals, adjacency, passes=smoothing_passes)

    face_normals, _ = triangle_normals_and_areas(points, triangles)
    # Orient the face normals exactly as the vertex normals were oriented, so
    # the visibility test compares like with like.
    flip = outward != (surface_signed_volume(points, triangles) > 0.0)
    if flip:
        face_normals = -face_normals

    # A prism only has positive volume when its base triangle is wound so the
    # winding normal points along the march. Marching inward therefore needs
    # the winding reversed, otherwise every column reads as inverted.
    march_triangles = triangles[:, ::-1].copy() if flip else triangles

    visibility = _visibility_factors(directions, triangles, face_normals)

    nominal_heights = np.array(
        [first_height * growth_rate**layer for layer in range(layers)]
    )

    warnings: list[str] = []
    all_points = [points.copy()]
    front = points.copy()
    thickness = np.zeros(vertex_count)
    frozen = np.zeros(vertex_count, dtype=bool)
    if tolerate_defects and diagnostic.non_manifold_vertices.size:
        frozen[diagnostic.non_manifold_vertices] = True
        warnings.append(
            f"{diagnostic.non_manifold_vertices.size} vertices on non-manifold "
            "edges frozen before marching"
        )
    layers_built = 0

    for layer_index in range(layers):
        nominal = nominal_heights[layer_index]
        proximity = _proximity_limits(front, adjacency)

        step = np.full(vertex_count, nominal) * visibility
        step = np.minimum(step, proximity)
        if max_thickness is not None:
            remaining = np.maximum(max_thickness - thickness, 0.0)
            step = np.minimum(step, remaining)
        step[frozen] = 0.0
        step = np.maximum(step, 0.0)

        candidate = front + directions * step[:, None]

        # Repair inverted prisms locally rather than globally. Halving every
        # vertex's step because one singular vertex (a cone apex, a knife
        # edge) misbehaves would throw away a perfectly good layer everywhere
        # else, so only the vertices belonging to inverted cells are backed
        # off, and on the final attempt they are frozen outright.
        base_points = np.concatenate(all_points, axis=0)
        layer_wedges = _layer_wedges(march_triangles, layers_built, vertex_count)
        accepted = None
        for attempt in range(_MAX_REPAIR_ATTEMPTS + 1):
            volumes = _wedge_volumes(
                np.concatenate([base_points, candidate], axis=0), layer_wedges
            )
            # A column whose three vertices are all stationary is collapsed by
            # design -- that is what freezing a defective region means -- so
            # its zero volume is expected rather than a failure. Such prisms
            # are dropped from the output further down.
            collapsed = np.all(step[march_triangles] <= 0.0, axis=1)
            inverted = (volumes <= 0.0) & ~collapsed
            if not np.any(inverted):
                accepted = candidate
                break
            if attempt == _MAX_REPAIR_ATTEMPTS:
                break
            offenders = np.unique(march_triangles[inverted])
            if attempt == _MAX_REPAIR_ATTEMPTS - 1:
                step[offenders] = 0.0
            else:
                step[offenders] *= 0.5
            candidate = front + directions * step[:, None]

        if accepted is None:
            warnings.append(
                f"layer {layer_index + 1} of {layers} could not be built with "
                "positive volume everywhere; stack terminated early"
            )
            break

        newly_frozen = (step <= 0.0) & ~frozen
        frozen |= step <= 0.0
        if np.any(newly_frozen):
            warnings.append(
                f"{int(np.count_nonzero(newly_frozen))} vertices frozen at "
                f"layer {layer_index + 1} by proximity or thickness limits"
            )

        thickness += step
        all_points.append(accepted)
        front = accepted
        layers_built += 1

        if np.all(frozen):
            warnings.append(
                "all vertices frozen; no further layers can be marched"
            )
            break

    if layers_built == 0:
        raise PrismExtrusionError(
            "could not build a single valid prism layer. Surface: "
            f"{diagnostic.describe()}. Either the requested first height "
            f"({first_height:.3e} m) is too large for this triangulation, or "
            "the surface has defects that fold the march over itself."
        )

    stacked_points = np.concatenate(all_points, axis=0)
    wedges = np.concatenate(
        [
            _layer_wedges(march_triangles, layer, vertex_count)
            for layer in range(layers_built)
        ],
        axis=0,
    )
    volumes = _wedge_volumes(stacked_points, wedges)
    # Drop the collapsed columns over frozen regions; everything that remains
    # must have strictly positive volume for SU2 to accept the mesh.
    live = volumes > 0.0
    dropped = int(np.count_nonzero(~live))
    if dropped:
        warnings.append(
            f"{dropped} collapsed prism columns over frozen vertices omitted"
        )
        wedges = wedges[live]
        volumes = volumes[live]
    if wedges.shape[0] == 0:
        raise PrismExtrusionError(
            "every prism column collapsed; no boundary layer could be built"
        )
    min_volume = float(volumes.min())

    outer_offset = layers_built * vertex_count
    outer_triangles = march_triangles + outer_offset

    if layers_built < layers:
        warnings.append(
            f"built {layers_built} of {layers} requested layers"
        )

    return PrismLayerResult(
        points=stacked_points,
        wedges=wedges,
        outer_triangles=outer_triangles,
        wall_triangles=triangles,
        layer_heights=nominal_heights[:layers_built],
        vertex_thickness=thickness,
        frozen_vertices=np.flatnonzero(frozen),
        min_wedge_volume=min_volume,
        layers_requested=layers,
        layers_built=layers_built,
        warnings=warnings,
        diagnostic=diagnostic,
    )


def _layer_wedges(
    triangles: np.ndarray, layer_index: int, vertex_count: int
) -> np.ndarray:
    """Prism connectivity joining layer ``layer_index`` to the next one.

    Node ordering follows the VTK/SU2 wedge convention: the three bottom
    vertices followed by the three top vertices.
    """
    bottom = triangles + layer_index * vertex_count
    top = triangles + (layer_index + 1) * vertex_count
    return np.concatenate([bottom, top], axis=1)


def suggested_max_thickness(
    points: np.ndarray, triangles: np.ndarray, fraction: float = 0.25
) -> float:
    """A safe total stack thickness for a surface, from its own scale.

    Returns a fraction of the shortest bounding-box dimension, which keeps the
    stack well inside the smallest feature the body has.
    """
    if points.size == 0:
        return 0.0
    extent = points.max(axis=0) - points.min(axis=0)
    positive = extent[extent > 0.0]
    if positive.size == 0:
        return 0.0
    return float(positive.min() * fraction)


def stack_height(first_height: float, layers: int, growth_rate: float) -> float:
    """Total height of a geometric prism stack (metres)."""
    if growth_rate == 1.0:
        return first_height * layers
    return first_height * (growth_rate**layers - 1.0) / (growth_rate - 1.0)


def layers_for_thickness(
    first_height: float, growth_rate: float, thickness: float
) -> int:
    """How many geometric layers fit inside a total thickness."""
    if thickness <= first_height:
        return 0
    if growth_rate == 1.0:
        return int(thickness // first_height)
    ratio = 1.0 + thickness * (growth_rate - 1.0) / first_height
    exact = math.log(ratio) / math.log(growth_rate)
    # Nudge before flooring so a thickness that is exactly N layers does not
    # round down to N-1 through floating-point error.
    return int(math.floor(exact + 1.0e-9))
