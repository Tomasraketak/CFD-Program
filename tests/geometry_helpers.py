"""Triangulated test geometry.

Two sources. Analytic builders (``icosphere``, ``unit_cube_surface``) are
exact, fast and dependency-free, so they carry the numerical assertions.
``surface_mesh_of`` goes through Gmsh/OpenCASCADE to produce the awkward
surface meshes that real CAD yields -- sharp rims, thin gaps, a singular cone
apex -- which is what the extruder actually has to survive.
"""

from __future__ import annotations

import functools

import numpy as np

# Gmsh-built meshes are slow enough to be worth caching across tests.
_MESH_CACHE: dict[tuple[str, float], tuple[np.ndarray, np.ndarray]] = {}


def unit_cube_surface() -> tuple[np.ndarray, np.ndarray]:
    """Closed, outward-wound triangulation of the unit cube."""
    points = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 1.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
            [1.0, 0.0, 1.0],
            [1.0, 1.0, 1.0],
            [0.0, 1.0, 1.0],
        ]
    )
    triangles = np.array(
        [
            [0, 3, 2], [0, 2, 1],  # z = 0
            [4, 5, 6], [4, 6, 7],  # z = 1
            [0, 1, 5], [0, 5, 4],  # y = 0
            [2, 3, 7], [2, 7, 6],  # y = 1
            [1, 2, 6], [1, 6, 5],  # x = 1
            [0, 4, 7], [0, 7, 3],  # x = 0
        ]
    )
    return points, triangles


@functools.lru_cache(maxsize=8)
def _icosphere_cached(
    subdivisions: int, radius: float
) -> tuple[tuple[float, ...], tuple[int, ...], int]:
    """Build an icosphere once per (subdivisions, radius) pair."""
    phi = (1.0 + 5.0**0.5) / 2.0
    vertices = [
        (-1, phi, 0), (1, phi, 0), (-1, -phi, 0), (1, -phi, 0),
        (0, -1, phi), (0, 1, phi), (0, -1, -phi), (0, 1, -phi),
        (phi, 0, -1), (phi, 0, 1), (-phi, 0, -1), (-phi, 0, 1),
    ]
    faces = [
        (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
        (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
        (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
        (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1),
    ]

    points = [np.array(v, dtype=float) for v in vertices]
    triangles = [tuple(f) for f in faces]

    for _ in range(subdivisions):
        midpoints: dict[tuple[int, int], int] = {}

        def midpoint(a: int, b: int) -> int:
            key = (a, b) if a < b else (b, a)
            if key not in midpoints:
                points.append((points[a] + points[b]) / 2.0)
                midpoints[key] = len(points) - 1
            return midpoints[key]

        refined = []
        for a, b, c in triangles:
            ab, bc, ca = midpoint(a, b), midpoint(b, c), midpoint(c, a)
            refined.extend(
                [(a, ab, ca), (b, bc, ab), (c, ca, bc), (ab, bc, ca)]
            )
        triangles = refined

    array = np.array(points)
    array = radius * array / np.linalg.norm(array, axis=1)[:, None]
    flat_triangles = tuple(int(i) for tri in triangles for i in tri)
    return tuple(array.ravel()), flat_triangles, len(triangles)


def icosphere(
    subdivisions: int = 2, radius: float = 1.0
) -> tuple[np.ndarray, np.ndarray]:
    """A geodesic sphere with outward-wound, near-uniform triangles.

    Parameters
    ----------
    subdivisions:
        Number of recursive subdivisions of the base icosahedron. Each one
        quadruples the triangle count (20, 80, 320, 1280, ...).
    radius:
        Sphere radius.

    Returns
    -------
    tuple of ndarray
        ``(N, 3)`` points and ``(T, 3)`` triangles.
    """
    flat_points, flat_triangles, triangle_count = _icosphere_cached(
        subdivisions, radius
    )
    points = np.array(flat_points).reshape(-1, 3)
    triangles = np.array(flat_triangles, dtype=np.int64).reshape(triangle_count, 3)
    return points.copy(), triangles.copy()


def _build_shape(name: str) -> None:
    """Create one OpenCASCADE primitive in the active Gmsh model."""
    import gmsh

    occ = gmsh.model.occ
    if name == "cylinder":
        occ.addCylinder(0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.05)
    elif name == "cone":
        # A mathematically sharp apex, which Gmsh meshes non-manifold.
        occ.addCone(0.0, 0.0, 0.0, 0.3, 0.0, 0.0, 0.0, 0.05)
    elif name == "plate":
        # Thin enough that opposing walls constrain the layer thickness.
        occ.addBox(0.0, 0.0, 0.0, 0.12, 0.004, 0.07)
    elif name == "torus":
        occ.addTorus(0.0, 0.0, 0.0, 0.2, 0.05)
    elif name == "sphere":
        occ.addSphere(0.0, 0.0, 0.0, 1.0)
    elif name == "finned":
        # A body tube with one fin through it: the concave corners at the fin
        # root are where a prism front folds over itself.
        body = occ.addCylinder(0.0, 0.0, 0.0, 0.3, 0.0, 0.0, 0.03)
        fin = occ.addBox(0.2, -0.004, -0.09, 0.1, 0.008, 0.18)
        occ.fuse([(3, body)], [(3, fin)])
    else:  # pragma: no cover - guarded by the caller
        raise ValueError(f"unknown test shape '{name}'")
    occ.synchronize()


def surface_mesh_of(
    shape: str, mesh_size: float
) -> tuple[np.ndarray, np.ndarray]:
    """Surface-mesh an OpenCASCADE primitive and return its triangulation.

    Parameters
    ----------
    shape:
        One of ``cylinder``, ``cone``, ``plate``, ``torus``, ``sphere``,
        ``finned``.
    mesh_size:
        Uniform target element size.

    Returns
    -------
    tuple of ndarray
        ``(N, 3)`` points and ``(T, 3)`` triangles with contiguous indices.
    """
    key = (shape, mesh_size)
    if key in _MESH_CACHE:
        points, triangles = _MESH_CACHE[key]
        return points.copy(), triangles.copy()

    import gmsh

    from backend.gmsh_session import gmsh_session

    with gmsh_session(f"test_{shape}"):
        _build_shape(shape)
        gmsh.option.setNumber("Mesh.MeshSizeMin", mesh_size)
        gmsh.option.setNumber("Mesh.MeshSizeMax", mesh_size)
        gmsh.model.mesh.generate(2)
        points, triangles = extract_surface_triangulation()

    _MESH_CACHE[key] = (points, triangles)
    return points.copy(), triangles.copy()


def extract_surface_triangulation(
    surface_tags: list[int] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Pull a contiguous triangulation out of the active Gmsh model.

    Gmsh node tags are sparse and one-based; they are remapped here to dense
    zero-based indices so the arrays can be used directly with NumPy.

    Parameters
    ----------
    surface_tags:
        Restrict extraction to these surface tags. ``None`` takes every
        surface in the model.

    Returns
    -------
    tuple of ndarray
        ``(N, 3)`` points and ``(T, 3)`` triangles.
    """
    import gmsh

    node_tags, coordinates, _ = gmsh.model.mesh.getNodes()
    points = np.array(coordinates).reshape(-1, 3)
    index_of = {int(tag): index for index, tag in enumerate(node_tags)}

    if surface_tags is None:
        surface_tags = [tag for _, tag in gmsh.model.getEntities(2)]

    blocks = []
    for tag in surface_tags:
        element_types, _, node_lists = gmsh.model.mesh.getElements(2, tag)
        for element_type, nodes in zip(element_types, node_lists):
            if element_type != 2:  # 3-node triangle
                continue
            indices = np.array([index_of[int(n)] for n in nodes], dtype=np.int64)
            blocks.append(indices.reshape(-1, 3))

    if not blocks:
        return points, np.empty((0, 3), dtype=np.int64)
    return points, np.concatenate(blocks)
