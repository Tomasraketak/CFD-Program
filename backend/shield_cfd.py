"""SU2 design points for the radiation-shield study.

What this module does, in order:

1. **Mesh** (:func:`mesh_shield_domain`): the shield STEP is centred in a
   ``2.0 x 2.0 x 1.44 m`` box (by default) and cut out of it with Gmsh; the
   fluid is filled with tetrahedra graded from the shield surface outwards.
   Faces are tagged INLET (upstream, -x), OUTLET (+x), BOTTOM (ground or
   roof, z = 0), TOP, SIDES, and the shield walls.
2. **Radiation** (:func:`radiation_facets`): SU2's incompressible solver has
   no surface-to-surface radiation, so the radiation a Fluent DO/S2S model
   would compute is worked out here, once per geometry, by ray casting on
   the shield surface: which facets the sun reaches (the louvres shade each
   other), and how much of the sky and of the ground each facet sees. Each
   facet's absorbed flux follows from that; its emission is fourth-power in
   its own temperature, so it is linearised about the wall temperature into
   SU2's convective wall condition ``h_r (T_eq - T_wall)``, which SU2 solves
   implicitly. The solve runs in *passes*, each re-linearising about the
   wall temperatures the previous one produced; the second pass typically
   moves them by thousandths of a kelvin. Facets are grouped by absorbed
   flux into a handful of SU2 markers.
3. **Solve** (:func:`run_design_point`): incompressible RANS with the energy
   equation, a velocity inlet, a 0 Pa pressure outlet, symmetry on the top
   and sides, and the bottom either a radiating ground (symmetry, its
   long-wave flux applied on the shield) or a roof held at a fixed
   temperature (an isothermal wall that also heats the passing air).
4. **Read** the air temperature at the thermometer point and subtract the
   inlet temperature.

Every pass writes its own configuration file, so a case directory can be
re-run by hand. ``python -m backend.shield_cfd <study_dir>`` solves every
design point of a prepared study on the computer it is run on.
"""

from __future__ import annotations

import json
import math
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from core.shield_models import (
    STEFAN_BOLTZMANN,
    BottomMode,
    DesignPoint,
    ShieldCfdSettings,
    ShieldSetup,
)

MARKER_INLET = "INLET"
MARKER_OUTLET = "OUTLET"
MARKER_BOTTOM = "BOTTOM"
MARKER_TOP = "TOP"
MARKER_SIDES = "SIDES"
SHIELD_PREFIX = "SHIELD_"

MESH_FILENAME = "shield_domain.su2"
FACETS_FILENAME = "radiation_facets.npz"
GEOMETRY_FILENAME = "geometry.json"

# Surface size on the shield and in the far field, metres, per resolution.
RESOLUTION_SIZES = {
    "coarse": (0.006, 0.12),
    "medium": (0.004, 0.08),
    "fine": (0.0025, 0.05),
}

# Spacing of the coarse surface the rays are cast against, metres (and at
# most a fifteenth of the shield, so a small shield keeps its shape).
RAY_SURFACE_SIZE = 0.02

# Cells across the narrowest air gap of the shield, per resolution. The
# sizes above suit the built-in 20 cm shield; a small shield with a 5 mm
# channel would otherwise get a single cell across the channel where the
# thermometer sits.
CELLS_ACROSS_GAP = {"coarse": 3.0, "medium": 4.0, "fine": 6.0}
# ... and at least this many cells along the shield's largest dimension.
CELLS_ALONG_SHIELD = {"coarse": 20.0, "medium": 30.0, "fine": 45.0}
SMALLEST_NEAR_SIZE = 4.0e-4


class ShieldCfdError(RuntimeError):
    """The shield CFD case could not be built, run or read."""


# ---------------------------------------------------------------------------
# Geometry and mesh
# ---------------------------------------------------------------------------


@dataclass
class ShieldDomain:
    """What meshing produced, in the solver frame (z up, wind along +x)."""

    mesh_path: Path
    shield_centre_m: list[float]
    shield_size_m: list[float]
    domain_size_m: list[float]
    cell_count: int
    node_count: int
    shield_triangles: np.ndarray  # (T, 3, 3) wall facets, fluid-facing normals
    marker_counts: dict[str, int] = field(default_factory=dict)
    near_size_m: float | None = None
    narrowest_gap_m: float | None = None
    sizing_note: str = ""

    def monitor_point(self, offset: Sequence[float]) -> list[float]:
        """The thermometer point in mesh coordinates."""
        return [c + o for c, o in zip(self.shield_centre_m, offset)]


def resolve_shield_step(setup: ShieldSetup, folder: Path) -> tuple[Path, float]:
    """The shield STEP to mesh and its scale, building the default one if needed."""
    if setup.shield_step_path:
        path = Path(setup.shield_step_path)
        if not path.is_file():
            raise ShieldCfdError(f"shield STEP file not found: {path}")
        if setup.scale_to_meters is not None:
            return path, setup.scale_to_meters
        from core.step_inspect import suggest_scale_to_meters

        return path, suggest_scale_to_meters(path).scale
    from backend.sample_geometry import create_radiation_shield_step

    path = create_radiation_shield_step(
        folder / "radiation_shield.step",
        size=tuple(setup.shield_size_m),
        plate_count=setup.plate_count,
    )
    return path, 1.0


def inspect_shield_step(path: Path | str, scale: float | None = None) -> dict:
    """Size (m) and number of solids of a shield STEP, as the mesher reads it."""
    import gmsh

    from backend.gmsh_session import gmsh_session

    path = Path(path)
    if scale is None:
        from core.step_inspect import suggest_scale_to_meters

        scale = suggest_scale_to_meters(path).scale
    with gmsh_session("shield_inspect"):
        occ = gmsh.model.occ
        solids = [e for e in occ.importShapes(str(path)) if e[0] == 3]
        occ.synchronize()
        if not solids:
            raise ShieldCfdError(f"{path.name} contains no solid; export the shield as a solid")
        low = np.full(3, np.inf)
        high = np.full(3, -np.inf)
        for dim, tag in solids:
            box = gmsh.model.getBoundingBox(dim, tag)
            low, high = np.minimum(low, box[:3]), np.maximum(high, box[3:])
    return {
        "size_m": [float(v) for v in (high - low) * scale],
        "solids": len(solids),
        "scale_to_meters": float(scale),
    }


def _shield_surface(step_path: Path, scale: float, spacing: float | None = None):
    """Triangulate the shield where the file puts it: (points, faces, size)."""
    import gmsh

    from backend.gmsh_session import gmsh_session

    with gmsh_session("shield_surface"):
        occ = gmsh.model.occ
        solids = [e for e in occ.importShapes(str(step_path)) if e[0] == 3]
        if not solids:
            raise ShieldCfdError(
                f"{Path(step_path).name} contains no solid; export the shield as a solid"
            )
        if scale != 1.0:
            occ.dilate(solids, 0.0, 0.0, 0.0, scale, scale, scale)
        occ.synchronize()
        low = np.full(3, np.inf)
        high = np.full(3, -np.inf)
        for dim, tag in solids:
            box = gmsh.model.getBoundingBox(dim, tag)
            low, high = np.minimum(low, box[:3]), np.maximum(high, box[3:])
        size = high - low
        spacing = spacing or float(max(size)) / 60.0
        gmsh.option.setNumber("Mesh.MeshSizeMax", spacing)
        gmsh.option.setNumber("Mesh.MeshSizeMin", 0.2 * spacing)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 12)
        gmsh.model.mesh.generate(2)
        tags, coords, _ = gmsh.model.mesh.getNodes()
        index_of = np.full(int(tags.max()) + 1, -1, dtype=np.int64)
        index_of[tags.astype(np.int64)] = np.arange(len(tags))
        points = coords.reshape(-1, 3)
        faces = []
        types, _, nodes = gmsh.model.mesh.getElements(2)
        for element_type, element_nodes in zip(types, nodes):
            if element_type == 2:
                faces.append(index_of[element_nodes.astype(np.int64)].reshape(-1, 3))
    return points, np.vstack(faces), size


def narrowest_air_gap(points: np.ndarray, faces: np.ndarray) -> float | None:
    """The narrowest air gap between parts of the shield, metres.

    From each facet a ray goes out into the air along its normal; the
    distance to where it meets the shield again is the gap there. The 5th
    percentile of those distances (not the minimum, which a single sliver
    facet in a corner would set) is the narrowest gap the mesh must resolve.
    None when no ray comes back (a single convex body).
    """
    import vtk

    faces = _orient_outwards(points, faces)
    triangles = points[faces]
    normals = _normals(triangles)
    centroids = triangles.mean(axis=1)
    occluder = _Occluder(triangles)
    reach = float(np.linalg.norm(np.ptp(points, axis=0)))
    found, cells = vtk.vtkPoints(), vtk.vtkIdList()
    gaps = []
    for centroid, normal in zip(centroids, normals):
        start = centroid + normal * 1.0e-6 * reach
        occluder._tree.IntersectWithLine(start, start + normal * reach, 1.0e-10, found, cells)
        if found.GetNumberOfPoints():
            hits = np.array([found.GetPoint(i) for i in range(found.GetNumberOfPoints())])
            distance = float(np.min(np.linalg.norm(hits - start, axis=1)))
            if distance > 1.0e-5 * reach:
                gaps.append(distance)
    if len(gaps) < 5:
        return None
    return float(np.percentile(gaps, 5))


def shield_mesh_sizes(
    resolution: str, shield_size: np.ndarray, gap: float | None
) -> tuple[float, float, str]:
    """Near-wall and far-field mesh size for this shield, and why.

    The table sizes are kept for a shield like the built-in one; a smaller
    shield or a narrow channel gets finer cells near the shield, so the
    channel the thermometer sits in is resolved rather than spanned by one
    cell.
    """
    near, far = RESOLUTION_SIZES[resolution]
    reasons = []
    along = float(max(shield_size)) / CELLS_ALONG_SHIELD[resolution]
    if along < near:
        near, reasons = along, [f"shield {max(shield_size) * 1000:.0f} mm"]
    if gap is not None and gap / CELLS_ACROSS_GAP[resolution] < near:
        near = gap / CELLS_ACROSS_GAP[resolution]
        reasons = [f"narrowest air gap {gap * 1000:.1f} mm"]
    near = max(near, SMALLEST_NEAR_SIZE)
    note = (
        f"near-shield cells {near * 1000:.2f} mm ({resolution}; set by "
        + (reasons[0] if reasons else "the resolution table")
        + ")"
    )
    return near, far, note


def mesh_shield_domain(
    setup: ShieldSetup,
    output_dir: Path | str,
    resolution: str = "coarse",
    threads: int = 0,
) -> ShieldDomain:
    """Cut the shield out of the box domain and mesh the air around it."""
    import gmsh

    from backend.gmsh_session import gmsh_session
    from backend.su2_mesh import VTK_TETRA, VTK_TRIANGLE, SU2Mesh

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    step_path, scale = resolve_shield_step(setup, output_dir)
    surface_points, surface_faces, shield_size = _shield_surface(step_path, scale)
    gap = narrowest_air_gap(surface_points, surface_faces)
    near, far, sizing_note = shield_mesh_sizes(resolution, shield_size, gap)
    length, width, height = setup.domain_size_m

    with gmsh_session("shield_domain", threads=threads or None):
        occ = gmsh.model.occ
        imported = occ.importShapes(str(step_path))
        solids = [entity for entity in imported if entity[0] == 3]
        if not solids:
            raise ShieldCfdError(
                f"{step_path.name} contains no solid; export the shield as a solid"
            )
        if scale != 1.0:
            occ.dilate(solids, 0.0, 0.0, 0.0, scale, scale, scale)
        occ.synchronize()
        low = np.full(3, np.inf)
        high = np.full(3, -np.inf)
        for dim, tag in solids:
            box = gmsh.model.getBoundingBox(dim, tag)
            low = np.minimum(low, box[:3])
            high = np.maximum(high, box[3:])
        size = high - low
        if np.any(size >= np.array(setup.domain_size_m) * 0.5):
            raise ShieldCfdError(
                f"the shield ({size.round(3).tolist()} m) is too large for the "
                f"domain {setup.domain_size_m} m -- check the CAD units"
            )
        if setup.shield_clearance_m is not None:
            # A set gap between the floor (roof, plate) and the shield's base.
            centre = np.array([0.0, 0.0, setup.shield_clearance_m + 0.5 * size[2]])
        else:
            centre = np.array([0.0, 0.0, 0.5 * height])
        shift = centre - 0.5 * (low + high)
        occ.translate(solids, *shift)

        box = occ.addBox(-0.5 * length, -0.5 * width, 0.0, length, width, height)
        fluid, _ = occ.cut([(3, box)], solids)
        occ.synchronize()
        if len(fluid) != 1:
            raise ShieldCfdError(
                f"cutting the shield out of the domain left {len(fluid)} air volumes"
            )
        volume = fluid[0][1]

        tolerance = 1.0e-6
        groups: dict[str, list[int]] = {
            MARKER_INLET: [], MARKER_OUTLET: [], MARKER_BOTTOM: [],
            MARKER_TOP: [], MARKER_SIDES: [], "SHIELD": [],
        }
        for _, surface in gmsh.model.getBoundary(fluid, oriented=False):
            x0, y0, z0, x1, y1, z1 = gmsh.model.getBoundingBox(2, surface)
            if x1 < -0.5 * length + tolerance:
                groups[MARKER_INLET].append(surface)
            elif x0 > 0.5 * length - tolerance:
                groups[MARKER_OUTLET].append(surface)
            elif z1 < tolerance:
                groups[MARKER_BOTTOM].append(surface)
            elif z0 > height - tolerance:
                groups[MARKER_TOP].append(surface)
            elif y1 < -0.5 * width + tolerance or y0 > 0.5 * width - tolerance:
                groups[MARKER_SIDES].append(surface)
            else:
                groups["SHIELD"].append(surface)
        empty = [name for name, tags in groups.items() if not tags]
        if empty:
            raise ShieldCfdError(f"no faces found for {', '.join(empty)}")

        field_distance = gmsh.model.mesh.field.add("Distance")
        gmsh.model.mesh.field.setNumbers(field_distance, "SurfacesList", groups["SHIELD"])
        gmsh.model.mesh.field.setNumber(field_distance, "Sampling", 20)
        threshold = gmsh.model.mesh.field.add("Threshold")
        gmsh.model.mesh.field.setNumber(threshold, "InField", field_distance)
        gmsh.model.mesh.field.setNumber(threshold, "SizeMin", near)
        gmsh.model.mesh.field.setNumber(threshold, "SizeMax", far)
        gmsh.model.mesh.field.setNumber(threshold, "DistMin", 2.0 * near)
        gmsh.model.mesh.field.setNumber(threshold, "DistMax", 0.6 * float(max(size)) + 8.0 * near)
        # A refined wake box downstream, where the warmed air goes.
        wake = gmsh.model.mesh.field.add("Box")
        gmsh.model.mesh.field.setNumber(wake, "VIn", 2.5 * near)
        gmsh.model.mesh.field.setNumber(wake, "VOut", far)
        gmsh.model.mesh.field.setNumber(wake, "XMin", centre[0] - 0.6 * size[0])
        gmsh.model.mesh.field.setNumber(wake, "XMax", centre[0] + 2.5 * size[0])
        gmsh.model.mesh.field.setNumber(wake, "YMin", -0.8 * size[1])
        gmsh.model.mesh.field.setNumber(wake, "YMax", 0.8 * size[1])
        gmsh.model.mesh.field.setNumber(wake, "ZMin", centre[2] - 0.8 * size[2])
        gmsh.model.mesh.field.setNumber(wake, "ZMax", centre[2] + 0.8 * size[2])
        gmsh.model.mesh.field.setNumber(wake, "Thickness", 0.3 * size[0])
        fields = [threshold, wake]
        if setup.floor_is_wall():
            # The floor heats the air: resolve its boundary layer under, upstream
            # of and behind the shield, up to above the shield's base.
            floor = gmsh.model.mesh.field.add("Box")
            gmsh.model.mesh.field.setNumber(floor, "VIn", 2.0 * near)
            gmsh.model.mesh.field.setNumber(floor, "VOut", far)
            gmsh.model.mesh.field.setNumber(floor, "XMin", centre[0] - 3.0 * size[0])
            gmsh.model.mesh.field.setNumber(floor, "XMax", centre[0] + 2.5 * size[0])
            gmsh.model.mesh.field.setNumber(floor, "YMin", -1.2 * size[1])
            gmsh.model.mesh.field.setNumber(floor, "YMax", 1.2 * size[1])
            gmsh.model.mesh.field.setNumber(floor, "ZMin", -1.0)
            gmsh.model.mesh.field.setNumber(floor, "ZMax", centre[2])
            gmsh.model.mesh.field.setNumber(floor, "Thickness", 0.5 * size[0])
            fields.append(floor)
        combined = gmsh.model.mesh.field.add("Min")
        gmsh.model.mesh.field.setNumbers(combined, "FieldsList", fields)
        gmsh.model.mesh.field.setAsBackgroundMesh(combined)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        # Small holes and posts keep enough segments to stay round.
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 12)
        gmsh.option.setNumber("Mesh.MeshSizeMin", 0.4 * near)
        gmsh.option.setNumber("Mesh.MeshSizeMax", far)
        gmsh.option.setNumber("Mesh.Algorithm", 6)
        gmsh.option.setNumber("Mesh.Optimize", 1)

        for name, tags in groups.items():
            gmsh.model.addPhysicalGroup(2, tags, name=name)
        gmsh.model.addPhysicalGroup(3, [volume], name="FLUID")
        failures = []
        # HXT is fast; Delaunay copes with the odd surface HXT refuses.
        for algorithm in (10, 1):
            gmsh.option.setNumber("Mesh.Algorithm3D", algorithm)
            try:
                gmsh.model.mesh.clear()
                gmsh.model.mesh.generate(3)
                if len(gmsh.model.mesh.getElements(3, volume)[0]) == 0:
                    raise RuntimeError("no tetrahedra were generated")
                break
            except Exception as error:  # noqa: BLE001 - retried, then reported
                failures.append(str(error))
        else:
            raise ShieldCfdError(
                "meshing the air round the shield failed: " + "; ".join(failures)
                + " -- try a finer mesh resolution, or check the STEP for "
                "overlapping or open surfaces"
            )

        node_tags, coords, _ = gmsh.model.mesh.getNodes()
        index_of = np.full(int(node_tags.max()) + 1, -1, dtype=np.int64)
        index_of[node_tags.astype(np.int64)] = np.arange(len(node_tags))
        points = coords.reshape(-1, 3)

        def triangles(surfaces: list[int]) -> np.ndarray:
            blocks = []
            for surface in surfaces:
                types, _, nodes = gmsh.model.mesh.getElements(2, surface)
                for element_type, element_nodes in zip(types, nodes):
                    if element_type == 2:
                        blocks.append(index_of[element_nodes.astype(np.int64)].reshape(-1, 3))
            return np.vstack(blocks) if blocks else np.empty((0, 3), dtype=np.int64)

        tets = []
        types, _, nodes = gmsh.model.mesh.getElements(3, volume)
        for element_type, element_nodes in zip(types, nodes):
            if element_type == 4:
                tets.append(index_of[element_nodes.astype(np.int64)].reshape(-1, 4))
        tets = np.vstack(tets)
        boundary = {name: triangles(tags) for name, tags in groups.items()}

    shield_faces = _orient_outwards(points, boundary.pop("SHIELD"))
    mesh = SU2Mesh(points=points)
    mesh.add_volume(VTK_TETRA, _positive_tets(points, tets))
    for name, faces in boundary.items():
        mesh.add_marker(name, VTK_TRIANGLE, faces)
    # The shield marker is split by radiation class later; keep it whole for now.
    mesh.add_marker("SHIELD", VTK_TRIANGLE, shield_faces)
    mesh_path = mesh.write(output_dir / "shield_domain_base.su2")
    np.save(output_dir / "shield_faces.npy", shield_faces)
    np.save(output_dir / "points.npy", points)
    domain = ShieldDomain(
        mesh_path=mesh_path,
        shield_centre_m=centre.tolist(),
        shield_size_m=size.tolist(),
        domain_size_m=list(setup.domain_size_m),
        cell_count=int(len(tets)),
        node_count=int(len(points)),
        shield_triangles=points[shield_faces],
        marker_counts=mesh.marker_counts(),
        near_size_m=near,
        narrowest_gap_m=gap,
        sizing_note=sizing_note,
    )
    (output_dir / GEOMETRY_FILENAME).write_text(
        json.dumps(
            {
                "shield_centre_m": domain.shield_centre_m,
                "shield_size_m": domain.shield_size_m,
                "domain_size_m": domain.domain_size_m,
                "cell_count": domain.cell_count,
                "node_count": domain.node_count,
                "marker_counts": domain.marker_counts,
                "shield_step": str(step_path),
                "scale_to_meters": scale,
                "resolution": resolution,
                "near_size_m": near,
                "narrowest_gap_m": gap,
                "sizing_note": sizing_note,
            },
            indent=2,
        )
    )
    return domain


def _positive_tets(points: np.ndarray, tets: np.ndarray) -> np.ndarray:
    """Tetrahedra with positive volume, as SU2 expects."""
    a, b, c, d = (points[tets[:, i]] for i in range(4))
    volume = np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a)
    tets = tets.copy()
    flip = volume < 0.0
    tets[flip, 1], tets[flip, 2] = tets[flip, 2], tets[flip, 1].copy()
    return tets


def _orient_outwards(points: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Turn every shield facet's normal to point into the air.

    A point just off the facet along its normal lies inside the solid when a
    ray from it crosses the shield surface an odd number of times.
    """
    triangles = points[faces]
    normals = _normals(triangles)
    centroids = triangles.mean(axis=1)
    edge = np.linalg.norm(triangles[:, 1] - triangles[:, 0], axis=1)
    # Well inside the thinnest wall: a plate is millimetres thick.
    probes = centroids + normals * (1.0e-3 * edge + 1.0e-7)[:, None]
    direction = np.array([0.5773, 0.5774, 0.5775])
    direction /= np.linalg.norm(direction)
    inside = _Occluder(triangles).crossings(probes, direction) % 2 == 1
    faces = faces.copy()
    faces[inside, 1], faces[inside, 2] = faces[inside, 2], faces[inside, 1].copy()
    return faces


# ---------------------------------------------------------------------------
# Radiation by ray casting
# ---------------------------------------------------------------------------


def _normals(triangles: np.ndarray) -> np.ndarray:
    normal = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    return normal / np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-300)


class _Occluder:
    """Ray queries against a triangulated surface, through a VTK cell locator.

    (vtkOBBTree crashes on real shield surfaces; the cell locator does not.)
    """

    def __init__(self, triangles: np.ndarray) -> None:
        import pyvista as pv
        import vtk

        points = triangles.reshape(-1, 3)
        faces = np.hstack(
            [np.full((len(triangles), 1), 3), np.arange(len(points)).reshape(-1, 3)]
        ).ravel()
        self._surface = pv.PolyData(points, faces)
        self._tree = vtk.vtkCellLocator()
        self._tree.SetDataSet(self._surface)
        self._tree.BuildLocator()
        self._points = vtk.vtkPoints()
        self._cells = vtk.vtkIdList()
        self.reach = 4.0 * float(np.linalg.norm(np.ptp(points, axis=0))) + 1.0

    def blocked(self, origins: np.ndarray, directions: np.ndarray) -> np.ndarray:
        """Does each ray hit the surface?"""
        ends = origins + directions * self.reach
        intersect = self._tree.IntersectWithLine
        found, cells = self._points, self._cells
        result = np.empty(len(origins), dtype=bool)
        for index in range(len(origins)):
            intersect(origins[index], ends[index], 1.0e-10, found, cells)
            result[index] = found.GetNumberOfPoints() > 0
        return result

    def first_hit(self, origins: np.ndarray, directions: np.ndarray) -> np.ndarray:
        """Index of the triangle each ray hits first, -1 for a free ray."""
        ends = origins + directions * self.reach
        found, cells = self._points, self._cells
        result = np.full(len(origins), -1, dtype=np.int64)
        for index in range(len(origins)):
            self._tree.IntersectWithLine(origins[index], ends[index], 1.0e-10, found, cells)
            count = found.GetNumberOfPoints()
            if count == 0:
                continue
            hits = np.array([found.GetPoint(k) for k in range(count)])
            nearest = int(np.argmin(np.linalg.norm(hits - origins[index], axis=1)))
            result[index] = cells.GetId(min(nearest, cells.GetNumberOfIds() - 1))
        return result

    def crossings(self, origins: np.ndarray, direction: np.ndarray) -> np.ndarray:
        """How many times each ray crosses the surface."""
        counts = np.empty(len(origins), dtype=np.int64)
        for index, origin in enumerate(origins):
            self._tree.IntersectWithLine(
                origin, origin + direction * self.reach, 1.0e-10, self._points, self._cells
            )
            counts[index] = self._points.GetNumberOfPoints()
        return counts


def point_inside_shield(triangles: np.ndarray, point) -> bool:
    """Is the point inside the shield material (odd number of crossings)?"""
    direction = np.array([0.5773, 0.5774, 0.5775])
    direction /= np.linalg.norm(direction)
    crossings = _Occluder(np.asarray(triangles)).crossings(
        np.asarray([point], dtype=float), direction
    )
    return bool(crossings[0] % 2 == 1)


def _hemisphere_directions(count: int, seed: int = 7) -> np.ndarray:
    """Cosine-weighted directions about +z, jittered-stratified.

    ``count`` is rounded up to a square so every stratum is used once.
    """
    rng = np.random.default_rng(seed)
    side = max(2, int(math.ceil(math.sqrt(count))))
    i, j = np.meshgrid(np.arange(side), np.arange(side), indexing="ij")
    u1 = ((i.ravel() + rng.random(side * side)) / side).clip(0.0, 1.0 - 1e-9)
    u2 = (j.ravel() + rng.random(side * side)) / side
    radius = np.sqrt(u1)
    angle = 2.0 * math.pi * u2
    return np.column_stack([radius * np.cos(angle), radius * np.sin(angle), np.sqrt(1.0 - u1)])


def _frame(normal: np.ndarray) -> np.ndarray:
    """Rotation taking +z onto ``normal``."""
    helper = np.array([1.0, 0.0, 0.0]) if abs(normal[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    tangent = np.cross(helper, normal)
    tangent /= np.linalg.norm(tangent)
    return np.column_stack([tangent, np.cross(normal, tangent), normal])


@dataclass
class RadiationFacets:
    """Per-facet radiation geometry of the shield, independent of the weather."""

    areas: np.ndarray      # (F,) m^2
    normals: np.ndarray    # (F, 3)
    sunlit: np.ndarray     # (F,) fraction of direct sun reaching the facet (0..1)
    sky_view: np.ndarray   # (F,) view factor to the sky
    ground_view: np.ndarray  # (F,) view factor to the ground / roof
    # (F,) True where the facet looks towards the thermometer (into the gaps).
    toward: np.ndarray | None = None
    # Long-wave exchange between the shield's own surfaces, on the ray
    # surface (coarse patches): view factors, which patch each facet
    # belongs to, and the patch areas.
    exchange: object | None = None
    patch_of: np.ndarray | None = None
    patch_areas: np.ndarray | None = None

    def set_exchange(self, matrix, patch_of: np.ndarray, patch_areas: np.ndarray) -> None:
        self.exchange = matrix
        self.patch_of = np.asarray(patch_of, dtype=np.int64)
        self.patch_areas = np.asarray(patch_areas, dtype=float)

    def has_exchange(self) -> bool:
        return self.exchange is not None and self.patch_of is not None

    def surroundings_view(self, plates: bool) -> np.ndarray:
        """Share of each facet's hemisphere it emits through, -.

        Without the exchange only the sky and ground take its emission
        (the rest of the hemisphere sees the shield, treated as a mirror);
        with it the facet emits into the whole hemisphere and gets back
        what the other surfaces send (:meth:`incoming`).
        """
        if plates and self.has_exchange():
            return np.ones(len(self.areas))
        return self.sky_view + self.ground_view

    def incoming(self, setup: ShieldSetup, wall_temp_k: np.ndarray) -> np.ndarray:
        """Long-wave each facet absorbs from the other shield surfaces, W/m^2.

        Grey diffuse surfaces: the radiosity of every patch,
        ``J = e sigma T^4 + (1 - e) F J``, is solved on the ray surface, and
        a facet absorbs ``e F J`` from the patches it sees.
        """
        from scipy.sparse import diags, identity
        from scipy.sparse.linalg import spsolve

        if not self.has_exchange():
            return np.zeros(len(self.areas))
        _, emissivity = self.optics(setup)
        patches = len(self.patch_areas)
        weight = np.bincount(self.patch_of, weights=self.areas, minlength=patches)
        empty = weight <= 0.0
        weight[empty] = 1.0

        def to_patches(values: np.ndarray) -> np.ndarray:
            mean = np.bincount(self.patch_of, weights=values * self.areas, minlength=patches) / weight
            mean[empty] = float(np.sum(values * self.areas) / np.sum(self.areas))
            return mean

        e_patch = to_patches(emissivity)
        t_patch = to_patches(np.asarray(wall_temp_k, dtype=float) ** 4) ** 0.25
        system = identity(patches, format="csr") - diags(1.0 - e_patch) @ self.exchange
        radiosity = spsolve(system.tocsc(), e_patch * STEFAN_BOLTZMANN * t_patch**4)
        irradiation = np.asarray(self.exchange @ radiosity).ravel()
        return emissivity * irradiation[self.patch_of]

    def optics(self, setup: ShieldSetup) -> tuple[np.ndarray, np.ndarray]:
        """Per-facet (solar absorptivity, emissivity) from which way it faces.

        With ``optics_orientation="vertical"`` facets looking up (n_z > 0.3)
        take the top-side optics, those looking down (n_z < -0.3) the bottom
        side's, the rest the mean. With ``"thermometer"`` the facets looking
        towards the thermometer take the bottom-side (inside) optics and all
        the others the top-side (outside) optics.
        """
        sides = setup.side_optics()
        if setup.optics_orientation == "thermometer" and self.toward is not None:
            inside = np.asarray(self.toward, dtype=bool)
            alpha = np.where(inside, sides["bottom"][0], sides["top"][0])
            emissivity = np.where(inside, sides["bottom"][1], sides["top"][1])
            return alpha.astype(float), emissivity.astype(float)
        nz = self.normals[:, 2]
        alpha = np.full(len(nz), sides["side"][0])
        emissivity = np.full(len(nz), sides["side"][1])
        up, down = nz > 0.3, nz < -0.3
        alpha[up], emissivity[up] = sides["top"]
        alpha[down], emissivity[down] = sides["bottom"]
        return alpha, emissivity

    def absorbed(self, setup: ShieldSetup, solar: float, bottom: float,
                 albedo: float | None = None) -> np.ndarray:
        """Absorbed flux per facet, W/m^2, for one condition."""
        alpha, emissivity = self.optics(setup)
        cosine = np.clip(self.normals[:, 2], 0.0, None)
        albedo = setup.ground_albedo if albedo is None else albedo
        return (
            alpha * solar * cosine * self.sunlit
            + alpha * albedo * solar * self.ground_view
            + emissivity * setup.sky_flux_w_m2() * self.sky_view
            + emissivity * bottom * self.ground_view
        )

    def emitted(self, setup: ShieldSetup, wall_temp_k: np.ndarray, plates: bool = False) -> np.ndarray:
        """Emission per facet, W/m^2: to the surroundings, or (with ``plates``)
        into the whole hemisphere, the shield's own surfaces included."""
        return (
            self.optics(setup)[1]
            * STEFAN_BOLTZMANN
            * wall_temp_k**4
            * self.surroundings_view(plates)
        )


def radiation_facets(
    triangles: np.ndarray, rays_per_face: int = 64, ray_surface: np.ndarray | None = None
) -> RadiationFacets:
    """Shading and view factors of every shield facet, by ray casting.

    ``triangles`` are the wall facets, normals into the air. With a coarser
    ``ray_surface`` of the same shield, the hemisphere rays (the expensive
    part) are cast from its facets and handed to the nearest fine facet
    facing the same way; the direct sun is still traced from every facet.
    """
    normals = _normals(triangles)
    areas = 0.5 * np.linalg.norm(
        np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]), axis=1
    )
    origins = _ray_origins(triangles, normals, areas)
    occluder = _Occluder(triangles if ray_surface is None else ray_surface)

    sun = np.array([0.0, 0.0, 1.0])
    facing = normals @ sun > 1.0e-6
    sunlit = np.zeros(len(triangles))
    if np.any(facing):
        blocked = occluder.blocked(origins[facing], np.broadcast_to(sun, (int(facing.sum()), 3)))
        sunlit[facing] = (~blocked).astype(float)

    if ray_surface is None:
        sky, ground, hits = _view_factors(origins, normals, occluder, rays_per_face, True)
        exchange = _exchange_matrix(hits, len(triangles))
        facets = RadiationFacets(areas, normals, sunlit, sky, ground)
        facets.set_exchange(exchange, np.arange(len(triangles)), areas)
        return facets
    else:
        from scipy.spatial import cKDTree

        coarse = _orient_triangles(ray_surface)
        coarse_normals = _normals(coarse)
        coarse_areas = 0.5 * np.linalg.norm(
            np.cross(coarse[:, 1] - coarse[:, 0], coarse[:, 2] - coarse[:, 0]), axis=1
        )
        coarse_origins = _ray_origins(coarse, coarse_normals, coarse_areas)
        coarse_sky, coarse_ground, hits = _view_factors(
            coarse_origins, coarse_normals, occluder, rays_per_face, True
        )
        # Offsetting along the normal keeps the two faces of a thin plate apart.
        lift = 0.01
        tree = cKDTree(coarse.mean(axis=1) + lift * coarse_normals)
        _, nearest = tree.query(triangles.mean(axis=1) + lift * normals)
        sky, ground = coarse_sky[nearest], coarse_ground[nearest]
    facets = RadiationFacets(areas, normals, sunlit, sky, ground)
    if ray_surface is not None:
        facets.set_exchange(_exchange_matrix(hits, len(coarse)), nearest, coarse_areas)
    return facets


def _exchange_matrix(hits: np.ndarray, count: int):
    """Sparse view factors F[i, j]: share of i's rays that land on j."""
    from scipy.sparse import coo_matrix

    rows = np.repeat(np.arange(len(hits)), hits.shape[1])
    cols = hits.ravel()
    keep = cols >= 0
    data = np.full(int(keep.sum()), 1.0 / hits.shape[1])
    return coo_matrix((data, (rows[keep], cols[keep])), shape=(len(hits), count)).tocsr()


def _exchange_arrays(facets: "RadiationFacets") -> dict:
    if not facets.has_exchange():
        return {}
    matrix = facets.exchange.tocsr()
    return {
        "exchange_data": matrix.data, "exchange_indices": matrix.indices,
        "exchange_indptr": matrix.indptr, "exchange_shape": np.array(matrix.shape),
        "patch_of": facets.patch_of, "patch_areas": facets.patch_areas,
    }


def facets_toward(triangles: np.ndarray, normals: np.ndarray, point) -> np.ndarray:
    """Which facets look towards ``point`` (normals point into the air)."""
    centroids = np.asarray(triangles).mean(axis=1)
    return np.einsum("ij,ij->i", np.asarray(point, dtype=float) - centroids, normals) > 0.0


def _ray_origins(triangles: np.ndarray, normals: np.ndarray, areas: np.ndarray) -> np.ndarray:
    size = np.sqrt(np.maximum(areas, 1e-12))
    return triangles.mean(axis=1) + normals * (1.0e-4 + 0.02 * size)[:, None]


def _view_factors(
    origins: np.ndarray, normals: np.ndarray, occluder: "_Occluder", rays: int,
    with_hits: bool = False,
):
    """Fractions of each facet's cosine-weighted hemisphere open to sky and ground.

    With ``with_hits`` also the occluder triangle every blocked ray lands on
    (``(F, rays)``, -1 for free rays): the facet-to-facet view factors for
    the radiation the plates exchange with each other.
    """
    local = _hemisphere_directions(rays)
    sky = np.zeros(len(origins))
    ground = np.zeros(len(origins))
    hits = np.full((len(origins), len(local)), -1, dtype=np.int64) if with_hits else None
    for start in range(0, len(origins), 256):
        stop = min(start + 256, len(origins))
        directions = np.concatenate([local @ _frame(normals[i]).T for i in range(start, stop)])
        starts = np.repeat(origins[start:stop], len(local), axis=0)
        if with_hits:
            landed = occluder.first_hit(starts, directions).reshape(stop - start, len(local))
            hits[start:stop] = landed
            free = landed < 0
        else:
            free = ~occluder.blocked(starts, directions).reshape(stop - start, len(local))
        up = directions[:, 2].reshape(stop - start, len(local)) > 0.0
        sky[start:stop] = np.mean(free & up, axis=1)
        ground[start:stop] = np.mean(free & ~up, axis=1)
    if with_hits:
        return sky, ground, hits
    return sky, ground


def _orient_triangles(triangles: np.ndarray) -> np.ndarray:
    """Triangles (as coordinates) with normals turned into the air."""
    points = triangles.reshape(-1, 3)
    faces = np.arange(len(points)).reshape(-1, 3)
    return points[_orient_outwards(points, faces)]


def coarse_ray_surface(setup: ShieldSetup, folder: Path) -> np.ndarray:
    """A coarse triangulation of the placed shield to cast rays against."""
    import gmsh

    from backend.gmsh_session import gmsh_session

    step_path, scale = resolve_shield_step(setup, folder)
    geometry = json.loads((folder / GEOMETRY_FILENAME).read_text())
    with gmsh_session("shield_rays"):
        occ = gmsh.model.occ
        solids = [e for e in occ.importShapes(str(step_path)) if e[0] == 3]
        if scale != 1.0:
            occ.dilate(solids, 0.0, 0.0, 0.0, scale, scale, scale)
        occ.synchronize()
        low = np.full(3, np.inf)
        high = np.full(3, -np.inf)
        for dim, tag in solids:
            box = gmsh.model.getBoundingBox(dim, tag)
            low = np.minimum(low, box[:3])
            high = np.maximum(high, box[3:])
        occ.translate(solids, *(np.array(geometry["shield_centre_m"]) - 0.5 * (low + high)))
        occ.synchronize()
        spacing = min(RAY_SURFACE_SIZE, float(max(high - low)) / 15.0)
        gmsh.option.setNumber("Mesh.MeshSizeMax", spacing)
        gmsh.option.setNumber("Mesh.MeshSizeMin", 0.2 * spacing)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 12)
        gmsh.model.mesh.generate(2)
        tags, coords, _ = gmsh.model.mesh.getNodes()
        index_of = np.full(int(tags.max()) + 1, -1, dtype=np.int64)
        index_of[tags.astype(np.int64)] = np.arange(len(tags))
        points = coords.reshape(-1, 3)
        faces = []
        types, _, nodes = gmsh.model.mesh.getElements(2)
        for element_type, element_nodes in zip(types, nodes):
            if element_type == 2:
                faces.append(index_of[element_nodes.astype(np.int64)].reshape(-1, 3))
    return points[np.vstack(faces)]


# ---------------------------------------------------------------------------
# Radiation classes -> SU2 markers
# ---------------------------------------------------------------------------


def classify(values: np.ndarray, classes: int) -> np.ndarray:
    """Group facets into ``classes`` bands of similar value (quantiles)."""
    if len(values) == 0:
        return np.zeros(0, dtype=np.int64)
    edges = np.unique(np.quantile(values, np.linspace(0.0, 1.0, classes + 1)[1:-1]))
    bands = np.searchsorted(edges, values, side="right")
    # Renumber so no class is empty: an empty SU2 marker is an error.
    return np.unique(bands, return_inverse=True)[1].astype(np.int64)


@dataclass
class PreparedMesh:
    """The mesh split into radiation-class markers, plus what the loop needs."""

    mesh_path: Path
    classes: np.ndarray        # (F,) class of each shield facet
    class_count: int
    facets: RadiationFacets
    wall_nodes: np.ndarray     # (F, 3) node indices of each facet
    points: np.ndarray


def write_class_mesh(
    study_dir: Path, facets: RadiationFacets, key: np.ndarray, classes: int
) -> PreparedMesh:
    """Write the SU2 mesh with the shield split into radiation classes."""
    faces = np.load(study_dir / "shield_faces.npy")
    points = np.load(study_dir / "points.npy")
    labels = classify(key, classes)
    count = write_marker_split(study_dir, labels, study_dir / MESH_FILENAME)
    return PreparedMesh(study_dir / MESH_FILENAME, labels, count, facets, faces, points)


def write_marker_split(study_dir: Path, labels: np.ndarray, mesh_path: Path) -> int:
    """Write the domain mesh with the shield facets grouped by ``labels``."""
    from backend.su2_mesh import VTK_TRIANGLE

    faces = np.load(study_dir / "shield_faces.npy")
    count = int(labels.max()) + 1 if len(labels) else 0
    base = (study_dir / "shield_domain_base.su2").read_text().splitlines()
    marker_start = next(i for i, line in enumerate(base) if line.startswith("NMARK="))
    body = base[:marker_start]
    markers = base[marker_start + 1 :]
    kept: list[str] = []
    index = 0
    kept_count = 0
    while index < len(markers):
        tag = markers[index].split("=", 1)[1].strip()
        elements = int(markers[index + 1].split("=", 1)[1])
        block = markers[index : index + 2 + elements]
        index += 2 + elements
        if tag != "SHIELD":
            kept.extend(block)
            kept_count += 1
    lines = body + [f"NMARK= {kept_count + count}"] + kept
    for label in range(count):
        chosen = faces[labels == label]
        lines.append(f"MARKER_TAG= {SHIELD_PREFIX}{label + 1}")
        lines.append(f"MARKER_ELEMS= {len(chosen)}")
        lines.extend(f"{VTK_TRIANGLE} {a} {b} {c}" for a, b, c in chosen)
    Path(mesh_path).write_text("\n".join(lines) + "\n", encoding="ascii")
    return count


# ---------------------------------------------------------------------------
# SU2 configuration
# ---------------------------------------------------------------------------


def build_shield_config(
    setup: ShieldSetup,
    cfd: ShieldCfdSettings,
    point: DesignPoint,
    marker_fluxes: dict[str, float | tuple[float, float]],
    iterations: int,
    restart: bool,
    mesh_filename: str = MESH_FILENAME,
) -> str:
    """SU2 incompressible RANS + energy configuration for one pass."""
    from core.atmosphere import R_SPECIFIC_AIR, isa_state

    ambient = setup.ambient_temp_k()
    state = isa_state(0.0)
    density = state.pressure_pa / (R_SPECIFIC_AIR * ambient)
    speed = point.wind_speed_ms
    lines: list[str] = []
    add = lines.append
    add("% ---- AeroThermalStudio - radiation shield design point ----")
    add(
        f"% {point.name}: wind {speed:g} m/s, solar {point.solar_flux_w_m2:g} W/m2, "
        f"bottom {point.bottom_flux_w_m2:g} W/m2 ({setup.bottom_mode.value})"
    )
    add("SOLVER= INC_RANS")
    add(f"KIND_TURB_MODEL= {cfd.turbulence_model}")
    add("MATH_PROBLEM= DIRECT")
    add(f"RESTART_SOL= {'YES' if restart else 'NO'}")
    add("")
    add("% ---- Fluid ----")
    add("INC_ENERGY_EQUATION= YES")
    if cfd.buoyancy:
        add("INC_DENSITY_MODEL= VARIABLE")
        add("FLUID_MODEL= INC_IDEAL_GAS")
        add(f"SPECIFIC_HEAT_CP= 1004.703")
        add("GRAVITY_FORCE= YES")
    else:
        add("INC_DENSITY_MODEL= CONSTANT")
        add("FLUID_MODEL= CONSTANT_DENSITY")
        add("SPECIFIC_HEAT_CP= 1004.703")
    add(f"INC_DENSITY_INIT= {density:.9f}")
    add(f"INC_VELOCITY_INIT= ( {speed:.6f}, 0.0, 0.0 )")
    add(f"INC_TEMPERATURE_INIT= {ambient:.6f}")
    add("INC_NONDIM= DIMENSIONAL")
    add(f"THERMODYNAMIC_PRESSURE= {state.pressure_pa:.3f}")
    add("VISCOSITY_MODEL= SUTHERLAND")
    add("MU_REF= 1.716E-5")
    add("MU_T_REF= 273.15")
    add("SUTHERLAND_CONSTANT= 110.4")
    add("CONDUCTIVITY_MODEL= CONSTANT_PRANDTL")
    add("PRANDTL_LAM= 0.71")
    add("TURBULENT_CONDUCTIVITY_MODEL= CONSTANT_PRANDTL_TURB")
    add("PRANDTL_TURB= 0.90")
    add("FREESTREAM_TURBULENCEINTENSITY= 0.05")
    add("FREESTREAM_TURB2LAMVISCRATIO= 10.0")
    add("")
    add("% ---- Boundaries ----")
    add("INC_INLET_TYPE= VELOCITY_INLET")
    add(f"MARKER_INLET= ( {MARKER_INLET}, {ambient:.6f}, {speed:.6f}, 1.0, 0.0, 0.0 )")
    add("INC_OUTLET_TYPE= PRESSURE_OUTLET")
    add(f"MARKER_OUTLET= ( {MARKER_OUTLET}, 0.0 )")
    symmetry = [MARKER_TOP, MARKER_SIDES]
    if setup.floor_is_wall():
        from backend.shield_study import bottom_state

        roof = bottom_state(setup, *point.inputs())[1]
        add(f"% floor ({setup.bottom_mode.value}) held at {roof:.2f} K")
        add(f"MARKER_ISOTHERMAL= ( {MARKER_BOTTOM}, {roof:.4f} )")
    else:
        symmetry.append(MARKER_BOTTOM)
    add(f"MARKER_SYM= ( {', '.join(symmetry)} )")
    fluxes = [
        f"{name}, {value:.5f}" for name, value in marker_fluxes.items()
        if not isinstance(value, tuple)
    ]
    transfer = [
        f"{name}, {value[0]:.6f}, {value[1]:.5f}" for name, value in marker_fluxes.items()
        if isinstance(value, tuple) and value[0] != "T"
    ]
    # Conjugate mode: the walls held at the solid's surface temperature.
    held = [
        f"{name}, {value[1]:.5f}" for name, value in marker_fluxes.items()
        if isinstance(value, tuple) and value[0] == "T"
    ]
    if held:
        if setup.floor_is_wall():
            # One MARKER_ISOTHERMAL line only: merge with the roof's.
            roof_line = next(i for i, line in enumerate(lines) if line.startswith("MARKER_ISOTHERMAL="))
            lines[roof_line] = lines[roof_line].rstrip(" )") + ", " + ", ".join(held) + " )"
        else:
            add(f"MARKER_ISOTHERMAL= ( {', '.join(held)} )")
    if fluxes:
        add(f"MARKER_HEATFLUX= ( {', '.join(fluxes)} )")
    if transfer:
        # Radiation linearised as h_r (T_eq - T_wall): see _marker_fluxes.
        add(f"MARKER_HEATTRANSFER= ( {', '.join(transfer)} )")
    walls = ", ".join(marker_fluxes)
    add(f"MARKER_PLOTTING= ( {walls} )")
    add(f"MARKER_MONITORING= ( {walls} )")
    add("")
    add("% ---- Numerics ----")
    add("NUM_METHOD_GRAD= GREEN_GAUSS")
    add("CONV_NUM_METHOD_FLOW= FDS")
    add("MUSCL_FLOW= YES")
    add("SLOPE_LIMITER_FLOW= VENKATAKRISHNAN")
    add("VENKAT_LIMITER_COEFF= 0.05")
    add("CONV_NUM_METHOD_TURB= SCALAR_UPWIND")
    add("MUSCL_TURB= NO")
    add("TIME_DISCRE_FLOW= EULER_IMPLICIT")
    add("TIME_DISCRE_TURB= EULER_IMPLICIT")
    add("CFL_NUMBER= 5.0")
    add("CFL_ADAPT= YES")
    # The energy equation is the slow one at low wind. A 1.05 ramp left it
    # around rms[T] -4 when a pass ran out of iterations, so each radiation
    # pass started from a half-converged wall temperature and the passes
    # crept (0.65, 0.20, 0.13 K) instead of settling. 1.1 reaches the
    # residual target (-7) in a few hundred iterations; a CFL ceiling much
    # above 100 diverges on these meshes.
    add("CFL_ADAPT_PARAM= ( 0.5, 1.1, 1.0, 100.0 )")
    add("LINEAR_SOLVER= FGMRES")
    add("LINEAR_SOLVER_PREC= ILU")
    add("LINEAR_SOLVER_ERROR= 1E-4")
    add("LINEAR_SOLVER_ITER= 20")
    add(f"ITER= {iterations}")
    add("CONV_FIELD= ( RMS_PRESSURE, RMS_TEMPERATURE )")
    add("CONV_RESIDUAL_MINVAL= -7")
    add("CONV_STARTITER= 50")
    add("")
    add("% ---- Files ----")
    add(f"MESH_FILENAME= {mesh_filename}")
    add("MESH_FORMAT= SU2")
    add("SOLUTION_FILENAME= solution_flow.dat")
    add("RESTART_FILENAME= restart_flow.dat")
    add("READ_BINARY_RESTART= YES")
    add("WRT_RESTART_OVERWRITE= YES")
    add("TABULAR_FORMAT= CSV")
    add("CONV_FILENAME= history")
    add("VOLUME_FILENAME= flow")
    add("SURFACE_FILENAME= surface_flow")
    add("OUTPUT_FILES= ( RESTART, PARAVIEW )")
    add("OUTPUT_WRT_FREQ= 200")
    add("SCREEN_OUTPUT= ( INNER_ITER, RMS_PRESSURE, RMS_TEMPERATURE, TOTAL_HEATFLUX )")
    add("SCREEN_WRT_FREQ_INNER= 10")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Running one design point
# ---------------------------------------------------------------------------


@dataclass
class DesignPointResult:
    """One solved design point."""

    point: DesignPoint
    monitor_temp_k: float
    delta_t_k: float
    shield_mean_temp_k: float
    passes: int
    converged: bool
    wall_time_s: float
    notes: list[str] = field(default_factory=list)
    wall_changes_k: list[float] = field(default_factory=list)
    radiation_settled: bool = True
    residual_drop_orders: float | None = None
    oscillating: bool = False


def load_study(study_dir: Path) -> tuple[ShieldSetup, ShieldCfdSettings, PreparedMesh, list[float]]:
    """Read back what :func:`prepare_study` wrote."""
    manifest = json.loads((study_dir / "study.json").read_text())
    setup = ShieldSetup.model_validate(manifest["setup"])
    cfd = ShieldCfdSettings.model_validate(manifest["cfd"])
    data = np.load(study_dir / FACETS_FILENAME)
    facets = RadiationFacets(
        data["areas"], data["normals"], data["sunlit"], data["sky_view"], data["ground_view"],
        data["toward"] if "toward" in data.files else None,
    )
    if "exchange_data" in data.files:
        from scipy.sparse import csr_matrix

        shape = tuple(int(v) for v in data["exchange_shape"])
        facets.set_exchange(
            csr_matrix((data["exchange_data"], data["exchange_indices"], data["exchange_indptr"]),
                       shape=shape),
            data["patch_of"], data["patch_areas"],
        )
    prepared = PreparedMesh(
        mesh_path=study_dir / MESH_FILENAME,
        classes=data["classes"],
        class_count=int(data["classes"].max()) + 1,
        facets=facets,
        wall_nodes=np.load(study_dir / "shield_faces.npy"),
        points=np.load(study_dir / "points.npy"),
    )
    geometry = json.loads((study_dir / GEOMETRY_FILENAME).read_text())
    monitor = [c + o for c, o in zip(geometry["shield_centre_m"], setup.thermometer_xyz_m)]
    return setup, cfd, prepared, monitor


def prepare_study(
    setup: ShieldSetup,
    cfd: ShieldCfdSettings,
    points: Sequence[DesignPoint],
    study_dir: Path | str,
    on_line: Callable[[str], None] | None = None,
) -> Path:
    """Mesh once, cast the radiation rays once, write the design-point list."""
    from backend.shield_study import write_design_points_csv

    study_dir = Path(study_dir)
    study_dir.mkdir(parents=True, exist_ok=True)
    say = on_line or (lambda _line: None)
    say(f"Meshing the air round the shield ({cfd.mesh_resolution})...")
    domain = mesh_shield_domain(setup, study_dir, cfd.mesh_resolution)
    say(f"  {domain.cell_count:,} cells, {len(domain.shield_triangles):,} shield facets")
    say(f"  {domain.sizing_note}")
    monitor = domain.monitor_point(setup.thermometer_xyz_m)
    if point_inside_shield(domain.shield_triangles, monitor):
        raise ShieldCfdError(
            f"the thermometer point {np.round(monitor, 4).tolist()} m lies inside the "
            "shield material; move it into the air with 'Thermometer' (offset from the "
            "centre of the shield's bounding box)"
        )
    if cfd.solid_conduction == "on":
        from backend.shield_conduction import mesh_solids, save_solid

        say("Meshing the shield plates for heat conduction...")
        step_path, scale = resolve_shield_step(setup, study_dir)
        solid = mesh_solids(
            step_path, scale, np.array(domain.shield_centre_m),
            domain.near_size_m or RESOLUTION_SIZES[cfd.mesh_resolution][0],
        )
        save_solid(study_dir, solid)
        say(f"  {len(solid['tets']):,} solid cells in {int(solid['body'].max()) + 1} bodies")
    say("Casting rays for shading and view factors...")
    occluders = coarse_ray_surface(setup, study_dir)
    facets = radiation_facets(domain.shield_triangles, cfd.rays_per_face, occluders)
    facets.toward = facets_toward(domain.shield_triangles, facets.normals, monitor)
    # Group by the baseline absorbed flux: facets that absorb alike, and see
    # the sky and ground alike, share a marker.
    baseline = facets.absorbed(setup, setup.solar_flux_w_m2, setup.baseline_bottom_flux())
    prepared = write_class_mesh(study_dir, facets, baseline, cfd.radiation_classes)
    np.savez(
        study_dir / FACETS_FILENAME,
        areas=facets.areas, normals=facets.normals, sunlit=facets.sunlit,
        sky_view=facets.sky_view, ground_view=facets.ground_view,
        toward=facets.toward, classes=prepared.classes,
        **_exchange_arrays(facets),
    )
    sunlit_area = float(np.sum(facets.areas * facets.sunlit * (facets.normals[:, 2] > 0)))
    say(
        f"  {prepared.class_count} radiation markers; sunlit area "
        f"{sunlit_area * 1e4:.0f} cm2 of {float(facets.areas.sum()) * 1e4:.0f} cm2"
    )
    (study_dir / "study.json").write_text(
        json.dumps(
            {"setup": setup.model_dump(mode="json"), "cfd": cfd.model_dump(mode="json")},
            indent=2,
        )
    )
    write_design_points_csv(study_dir / "design_points.csv", setup, points)
    _write_run_scripts(study_dir)
    return study_dir


def _write_run_scripts(study_dir: Path) -> None:
    """Scripts that solve every design point on the computer they run on."""
    root = Path(__file__).resolve().parent.parent
    python = sys.executable or "python"
    (study_dir / "run_design_points.bat").write_text(
        "@echo off\r\n"
        f'cd /d "{root}"\r\n'
        f'"{python}" -m backend.shield_cfd "%~dp0."\r\n'
        "pause\r\n"
    )
    (study_dir / "run_design_points.sh").write_text(
        "#!/bin/sh\n"
        f'cd "{root}"\n'
        f'"{python}" -m backend.shield_cfd "$(dirname "$0")"\n'
    )


def _wall_temperatures(
    case_dir: Path, prepared: PreparedMesh, fallback: float
) -> np.ndarray:
    """Mean temperature of every shield facet from the volume solution."""
    from scipy.spatial import cKDTree

    from backend.visualizer import find_solution_file, load_solution, resolve_field

    solution = load_solution(find_solution_file(case_dir))
    name = resolve_field(solution, "temperature")
    values = np.asarray(solution.point_data[name]).ravel()
    tree = cKDTree(np.asarray(solution.points))
    corners = prepared.points[prepared.wall_nodes].reshape(-1, 3)
    _, index = tree.query(corners)
    temps = values[index].reshape(-1, 3).mean(axis=1)
    temps[~np.isfinite(temps)] = fallback
    return temps


def _marker_fluxes(
    setup: ShieldSetup, prepared: PreparedMesh, absorbed: np.ndarray, wall: np.ndarray,
    plates: bool = False,
) -> dict[str, float | tuple[float, float]]:
    """Radiation boundary condition per marker.

    The net radiative gain ``q_abs - e*sigma*F*T^4`` is linearised about the
    current wall temperature ``T0`` into a convective form SU2 solves
    implicitly with the wall temperature:
    ``q = h_r (T_eq - T)``, ``h_r = 4 e sigma F T0^3``,
    ``T_eq = T0 + (q_abs - e sigma F T0^4) / h_r`` -- a Newton step on the
    fourth-power term, so a pass or two settles it. Returns ``(h_r, T_eq)``
    per marker, or a plain heat flux for a marker that sees no surroundings.
    """
    facets = prepared.facets
    view = facets.surroundings_view(plates)
    h_r = 4.0 * facets.optics(setup)[1] * STEFAN_BOLTZMANN * view * wall**3
    # What the other plates send is taken at the last pass's wall
    # temperature; the facet's own emission stays implicit.
    received = facets.incoming(setup, wall) if plates else 0.0
    net = absorbed + received - facets.emitted(setup, wall, plates)
    conditions: dict[str, float | tuple[float, float]] = {}
    for label in range(prepared.class_count):
        chosen = prepared.classes == label
        area = facets.areas[chosen]
        total = max(float(np.sum(area)), 1e-300)
        coefficient = float(np.sum(h_r[chosen] * area)) / total
        flux = float(np.sum(net[chosen] * area)) / total
        name = f"{SHIELD_PREFIX}{label + 1}"
        if coefficient < 1.0e-3:
            conditions[name] = flux
        else:
            # Area-weighted mean wall temperature of the class, and the
            # equilibrium temperature that reproduces the net gain there.
            mean_wall = float(np.sum(h_r[chosen] * area * wall[chosen])) / (coefficient * total)
            conditions[name] = (coefficient, mean_wall + flux / coefficient)
    return conditions


def run_design_point(
    study_dir: Path | str,
    point: DesignPoint,
    runner,
    on_line: Callable[[str], None] | None = None,
) -> DesignPointResult:
    """Solve one design point in ``study_dir/points/<name>``."""
    from backend.shield_study import ShieldAnalyticModel
    from backend.su2_config import write_config
    from backend.su2_parser import SU2OutputParser
    from backend.visualizer import find_solution_file, load_solution, probe_point_value

    study_dir = Path(study_dir)
    setup, cfd, prepared, monitor = load_study(study_dir)
    case_dir = study_dir / "points" / point.name
    case_dir.mkdir(parents=True, exist_ok=True)
    local_mesh = case_dir / MESH_FILENAME
    if not local_mesh.exists():
        shutil.copyfile(prepared.mesh_path, local_mesh)

    ambient = setup.ambient_temp_k()
    lumped = ShieldAnalyticModel(setup).solve(*point.inputs())
    wall = np.full(len(prepared.classes), lumped.shield_temp_k)
    from backend.shield_study import bottom_state

    bottom_flux, floor_temp, albedo = bottom_state(setup, *point.inputs())
    absorbed = prepared.facets.absorbed(setup, point.solar_flux_w_m2, bottom_flux, albedo)
    started = time.perf_counter()
    converged = False
    notes: list[str] = []
    if setup.sky_note():
        notes.append(setup.sky_note())
    wall_changes: list[float] = []
    behaviour: dict = {"residual_drop_orders": None, "oscillating": False}
    say = on_line or (lambda _line: None)
    plates = cfd.plate_radiation == "on" and prepared.facets.has_exchange()
    if cfd.plate_radiation == "on" and not plates:
        notes.append("radiation between the plates is not included: this study was "
                      "prepared before it existed; prepare it again")
    coupling = _ConjugateCoupling.create(
        study_dir, setup, cfd, prepared, absorbed, lumped, plates
    )
    if coupling is not None:
        say(f"{point.name}: conjugate heat transfer -- conduction in the shield plates")
        wall = coupling.target

    for index in range(cfd.radiation_passes):
        if coupling is not None:
            fluxes = coupling.markers(case_dir / MESH_FILENAME, cfd.radiation_classes)
        else:
            fluxes = _marker_fluxes(setup, prepared, absorbed, wall, plates)
        restart = index > 0
        if restart:
            shutil.copyfile(case_dir / "restart_flow.dat", case_dir / "solution_flow.dat")
        iterations = cfd.iterations_first_pass if index == 0 else cfd.iterations_later_passes
        text = build_shield_config(setup, cfd, point, fluxes, iterations, restart)
        config = write_config(text, case_dir / f"pass_{index + 1}.cfg")
        say(f"{point.name}: radiation pass {index + 1}/{cfd.radiation_passes}")
        parser = SU2OutputParser()

        def handle(line: str) -> None:
            parser.feed(line)
            if on_line is not None:
                on_line(line)

        outcome = runner.run(
            config_path=config,
            working_directory=case_dir,
            ranks=cfd.mpi_ranks,
            on_line=handle,
        )
        if parser.errors:
            raise ShieldCfdError(
                f"{point.name}: SU2 reported an error:\n" + "\n".join(parser.errors[:5])
            )
        if not outcome.succeeded:
            raise ShieldCfdError(
                f"{point.name}: SU2 stopped with code {outcome.return_code}:\n"
                + "\n".join(outcome.tail(15))
            )
        # Stopping short of the limit means SU2's residual criterion was met.
        converged = bool(parser.records) and parser.records[-1].iteration < iterations - 1
        areas = prepared.facets.areas
        if coupling is not None:
            # The air answered; let the plates answer back. The change of
            # their surface temperature is the coupling's convergence.
            previous = coupling.target
            coupling.update_from_air(case_dir, ambient)
            wall = coupling.target
        else:
            previous = wall
            wall = _wall_temperatures(case_dir, prepared, lumped.shield_temp_k)
        # No relaxation needed: the linearised emission is a Newton step.
        change = float(np.sum(np.abs(wall - previous) * areas) / np.sum(areas))
        wall_changes.append(round(change, 4))
        behaviour = residual_behaviour(parser.records, iterations)
        say(f"{point.name}: pass {index + 1} mean wall temperature change {change:.3f} K")
        if (index > 0 or coupling is not None) and change < RADIATION_SETTLED_K:
            break

    solution = load_solution(find_solution_file(case_dir))
    try:
        monitor_temp = probe_point_value(solution, monitor, "temperature")
    except Exception as error:  # noqa: BLE001 - reported with context
        raise ShieldCfdError(
            f"{point.name}: the thermometer point {monitor} could not be read "
            f"({error}); is it inside a solid part of the shield?"
        ) from error
    shield_mean = float(
        np.sum(wall * prepared.facets.areas) / np.sum(prepared.facets.areas)
    )
    if not converged:
        notes.append("the last pass reached its iteration limit")
    # The first pass starts from the lumped-model wall temperature, so its
    # change says nothing about settling; a single-pass run has nothing to
    # compare and is reported as settled only if asked for one pass.
    settled = cfd.radiation_passes == 1 or (
        len(wall_changes) > 1 and wall_changes[-1] < RADIATION_SETTLED_K
    )
    if not settled:
        notes.append(
            f"radiation not settled: the last pass changed the wall by "
            f"{wall_changes[-1]:.3f} K (target < {RADIATION_SETTLED_K} K); raise radiation_passes"
        )
    if behaviour["oscillating"]:
        notes.append(
            "residuals oscillate in the last quarter of the iterations: the flow is "
            "probably unsteady here (often at low wind); treat this dT as an estimate "
            "of the time average"
        )
    result = DesignPointResult(
        point=point,
        monitor_temp_k=monitor_temp,
        delta_t_k=monitor_temp - ambient,
        shield_mean_temp_k=shield_mean,
        passes=index + 1,
        converged=converged,
        wall_time_s=time.perf_counter() - started,
        notes=notes,
        wall_changes_k=wall_changes,
        radiation_settled=settled,
        residual_drop_orders=behaviour["residual_drop_orders"],
        oscillating=behaviour["oscillating"],
    )
    (case_dir / "result.json").write_text(
        json.dumps(
            {
                "name": point.name,
                "wind_speed_ms": point.wind_speed_ms,
                "solar_flux_w_m2": point.solar_flux_w_m2,
                "bottom_flux_w_m2": point.bottom_flux_w_m2,
                "monitor_temp_k": result.monitor_temp_k,
                "delta_t_k": result.delta_t_k,
                "shield_mean_temp_k": result.shield_mean_temp_k,
                "passes": result.passes,
                "converged": result.converged,
                "wall_changes_k": result.wall_changes_k,
                "radiation_settled": result.radiation_settled,
                "residual_drop_orders": result.residual_drop_orders,
                "oscillating": result.oscillating,
                "mesh_resolution": cfd.mesh_resolution,
                "conduction": coupling is not None,
                "floor_temperature_k": floor_temp,
                "bottom_flux_used_w_m2": bottom_flux,
                "plate_radiation": plates,
                "solid_temperature_k": (
                    None if coupling is None else coupling.body_temperatures()
                ),
                "wall_time_s": result.wall_time_s,
                "notes": notes,
            },
            indent=2,
        )
    )
    return result


RADIATION_SETTLED_K = 0.05


def residual_behaviour(records, iterations: int | None = None) -> dict:
    """How the residuals of one SU2 run behaved.

    ``residual_drop_orders`` is how many decades the leading residual fell
    from the first iteration to the last. ``oscillating`` flags a run whose
    last quarter shows no net fall (under 0.2 decades) while swinging by
    more than half a decade -- the signature of an unsteady flow (vortex
    shedding at low wind) that a steady solver cannot settle.
    """
    import math

    names = ("rms_pressure", "rms_rho", "rms_velocity_x", "rms_rho_u")
    series: list[float] = []
    for name in names:
        series = [r.get(name) for r in records if math.isfinite(r.get(name))]
        if len(series) >= 8:
            break
    if len(series) < 8:
        candidates = sorted({k for r in records for k in r.values if k.startswith("rms")})
        for name in candidates:
            series = [r.get(name) for r in records if math.isfinite(r.get(name))]
            if len(series) >= 8:
                break
    if len(series) < 8:
        return {"residual_drop_orders": None, "oscillating": False}
    tail = series[-max(4, len(series) // 4):]
    half = len(tail) // 2
    # Net fall between the two halves of the tail, averaged so that where
    # an oscillation happens to stand at either end does not count.
    net = sum(tail[:half]) / half - sum(tail[half:]) / (len(tail) - half)
    swing = max(tail) - min(tail)
    return {
        "residual_drop_orders": round(series[0] - series[-1], 3),
        "oscillating": bool(net < 0.2 and swing > 0.5),
    }


class _ConjugateCoupling:
    """The solid side of the conjugate loop for one design point.

    Holds the plates' conduction model and what the air did at each shield
    facet in the last pass; produces the isothermal wall markers for the
    next air solve.
    """

    def __init__(self, conduction, solid, prepared, absorbed, emissivity, view, h_air, start,
                 setup=None, plates=False):
        from backend.shield_conduction import match_surfaces

        facet_centroids = prepared.points[prepared.wall_nodes].mean(axis=1)
        self.conduction = conduction
        self.body = solid["body"]
        self.prepared = prepared
        self.to_solid = match_surfaces(facet_centroids, conduction.centroids)
        self.to_fluid = match_surfaces(conduction.centroids, facet_centroids)
        self.absorbed = absorbed
        self.emissivity = emissivity
        self.view = view
        count = len(facet_centroids)
        self.h_air = np.full(count, h_air)
        self.t_wall = np.full(count, start)
        self.q_air = self.h_air * 0.0
        self.nodes = np.full(len(conduction.points), start)
        self.ambient = start
        self.target = np.full(count, start)
        self.setup = setup
        self.plates = plates

    @classmethod
    def create(cls, study_dir, setup, cfd, prepared, absorbed, lumped, plates=False):
        from backend.shield_conduction import SolidConduction, load_solid

        if cfd.solid_conduction != "on":
            return None
        solid = load_solid(study_dir)
        if solid is None:
            return None
        conduction = SolidConduction(solid["points"], solid["tets"], setup.shield_conductivity_w_mk)
        _, emissivity = prepared.facets.optics(setup)
        view = prepared.facets.surroundings_view(plates)
        coupling = cls(
            conduction, solid, prepared, absorbed, emissivity, view,
            lumped.external_h_w_m2k, lumped.shield_temp_k, setup, plates,
        )
        ambient = setup.ambient_temp_k()
        # The first guess of the air side: the lumped model's film
        # coefficient against the inlet air.
        coupling.t_wall = np.full(len(view), ambient)
        coupling.q_air = np.zeros(len(view))
        coupling._solve_solid()
        return coupling

    def _solve_solid(self) -> None:
        from backend.shield_conduction import SolidSurfaceState

        m = self.to_solid
        absorbed = self.absorbed
        if self.plates:
            # The other plates' long-wave at the current surface temperature.
            absorbed = absorbed + self.prepared.facets.incoming(self.setup, self.target)
        state = SolidSurfaceState(
            absorbed=absorbed[m], emissivity=self.emissivity[m], view=self.view[m],
            q_air=self.q_air[m], t_air_wall=self.t_wall[m], h_air=self.h_air[m],
        )
        self.nodes = self.conduction.solve(state, float(np.mean(self.target)))
        surface = self.conduction.surface_temperature(self.nodes)
        self.target = surface[self.to_fluid]

    def markers(self, mesh_path: Path, classes: int) -> dict:
        """Group the facets by solid temperature into isothermal markers."""
        labels = classify(self.target, classes)
        count = write_marker_split(self.prepared.mesh_path.parent, labels, mesh_path)
        areas = self.prepared.facets.areas
        conditions = {}
        for label in range(count):
            chosen = labels == label
            mean = float(np.sum(self.target[chosen] * areas[chosen]) / np.sum(areas[chosen]))
            conditions[f"{SHIELD_PREFIX}{label + 1}"] = ("T", mean)
        return conditions

    def update_from_air(self, case_dir: Path, ambient: float) -> None:
        """Read the air's wall heat flux and re-solve the plates."""
        from backend.shield_conduction import film_coefficient

        t_wall, q_raw = _wall_heat_flux(case_dir, self.prepared)
        areas = self.prepared.facets.areas
        # SU2's sign convention for Heat_Flux is read from the solution
        # itself: heat leaves walls hotter than the air and enters colder
        # ones, so the flux into the air correlates positively with
        # (T_wall - T_ambient).
        sign = 1.0 if np.sum(q_raw * areas * (t_wall - ambient)) >= 0.0 else -1.0
        q_air = sign * q_raw
        self.h_air = film_coefficient(q_air, t_wall, ambient, self.h_air)
        self.q_air = q_air
        self.t_wall = t_wall
        self._solve_solid()

    def body_temperatures(self) -> list[dict]:
        """Lowest, mean and highest temperature of every shield body, K."""
        out = []
        for body in np.unique(self.body):
            nodes = np.unique(self.conduction.tets[self.body == body]) if len(
                self.conduction.tets
            ) == len(self.body) else np.arange(len(self.nodes))
            values = self.nodes[nodes]
            out.append({
                "body": int(body) + 1, "min_k": round(float(values.min()), 3),
                "mean_k": round(float(values.mean()), 3), "max_k": round(float(values.max()), 3),
            })
        return out


def _wall_heat_flux(case_dir: Path, prepared: PreparedMesh) -> tuple[np.ndarray, np.ndarray]:
    """Wall temperature and SU2's wall heat flux at every shield facet."""
    from scipy.spatial import cKDTree

    from backend.visualizer import find_solution_file, load_solution, resolve_field

    solution = load_solution(find_solution_file(case_dir))
    temperature = np.asarray(solution.point_data[resolve_field(solution, "temperature")]).ravel()
    flux = np.asarray(solution.point_data[resolve_field(solution, "heat_flux")]).ravel()
    _, index = cKDTree(np.asarray(solution.points)).query(
        prepared.points[prepared.wall_nodes].reshape(-1, 3)
    )
    t_wall = temperature[index].reshape(-1, 3).mean(axis=1)
    q_raw = flux[index].reshape(-1, 3).mean(axis=1)
    return t_wall, q_raw


def solved_points(study_dir: Path | str) -> list[DesignPoint]:
    """The study's design points, with the results solved so far filled in."""
    from backend.shield_study import read_design_points_csv

    study_dir = Path(study_dir)
    manifest = json.loads((study_dir / "study.json").read_text())
    setup = ShieldSetup.model_validate(manifest["setup"])
    points = read_design_points_csv(study_dir / "design_points.csv", setup)
    filled = []
    for point in points:
        result = study_dir / "points" / point.name / "result.json"
        if point.delta_t_k is None and result.is_file():
            data = json.loads(result.read_text())
            point = point.model_copy(update={"delta_t_k": data["delta_t_k"], "source": "su2"})
        filled.append(point)
    return filled


def run_study_points(
    study_dir: Path | str,
    runner,
    on_line: Callable[[str], None] | None = None,
    only_unsolved: bool = True,
) -> list[DesignPoint]:
    """Solve every (unsolved) design point of a prepared study in turn."""
    study_dir = Path(study_dir)
    points = solved_points(study_dir)
    for point in points:
        if only_unsolved and point.delta_t_k is not None:
            continue
        run_design_point(study_dir, point.model_copy(update={"delta_t_k": None}), runner, on_line)
    return solved_points(study_dir)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m backend.shield_cfd <study_dir>``: solve every design point."""
    from backend.runner import SU2Runner

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print("usage: python -m backend.shield_cfd <study_dir>")
        return 2
    study_dir = Path(arguments[0])
    points = run_study_points(study_dir, SU2Runner(), on_line=print)
    solved = [p for p in points if p.delta_t_k is not None]
    print(f"{len(solved)} of {len(points)} design points solved")
    for point in solved:
        print(f"  {point.name}: dT = {point.delta_t_k:+.3f} K")
    return 0


if __name__ == "__main__":  # pragma: no cover - command line entry
    raise SystemExit(main())
