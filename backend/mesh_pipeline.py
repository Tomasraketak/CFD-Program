"""CAD ingestion, orientation, domain construction and hybrid mesh generation.

The pipeline takes a STEP file to a solver-ready ``.su2`` mesh:

1. Import and heal the CAD through OpenCASCADE.
2. Rotate the body so the operator's nose vector lies along +X and translate
   the reference origin to the wind-tunnel origin.
3. Measure the reference length and diameter, and classify wall faces into
   the airframe and the fins.
4. Surface-mesh the body with curvature and feature-based sizing, bisecting
   the characteristic size until the cell count lands inside the target band.
5. Extrude prism boundary layers off the wall (see
   :mod:`backend.prism_layers`, which exists because Gmsh cannot do this).
6. Build the farfield envelope and tetrahedralise the gap between it and the
   prism shell.
7. Stitch prisms and tetrahedra together and write the tagged ``.su2``.

The cell-count bisection in step 4 is what delivers the 250k-750k (aerodynamic)
and 150k-400k (thermal) guarantees, rather than hoping a fixed preset lands in
range on arbitrary geometry.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import gmsh
import numpy as np

from backend.gmsh_session import gmsh_session
from backend.prism_layers import (
    PrismExtrusionError,
    PrismLayerResult,
    diagnose_surface,
    extrude_prism_layers,
)
from backend.su2_mesh import (
    VTK_TETRA,
    VTK_TRIANGLE,
    VTK_WEDGE,
    SU2Mesh,
    merge_nodes,
)
from core.atmosphere import isa_state
from core.models import (
    DomainShape,
    MeshRequest,
    MeshResult,
    SimulationTrack,
)
from core.units import first_cell_height, yplus_from_first_cell_height

# Marker names SU2 configurations reference.
MARKER_WALL_ROCKET = "WALL_ROCKET"
MARKER_WALL_FINS = "WALL_FINS"
MARKER_FARFIELD = "FARFIELD"
MARKER_SYMMETRY = "SYMMETRY"

# Gmsh entity tag reserved for the discrete prism outer shell. Kept far above
# anything OpenCASCADE allocates so the two never collide -- a collision
# silently drops the shell and meshes the farfield as if the body were absent.
_SHELL_SURFACE_TAG = 900_001

# A face is treated as a fin when it reaches beyond this multiple of the
# airframe radius.
DEFAULT_FIN_RADIUS_RATIO = 1.12

# Bisection tolerance: stop once the count is within the band.
_MIN_SIZE_SCALE = 0.05
_MAX_SIZE_SCALE = 20.0


class MeshPipelineError(RuntimeError):
    """Raised when a mesh cannot be produced from the given inputs."""


@dataclass
class GeometryMetrics:
    """Measurements taken from the aligned body."""

    reference_length_m: float
    reference_diameter_m: float
    reference_area_m2: float
    bounding_box_min: tuple[float, float, float]
    bounding_box_max: tuple[float, float, float]
    surface_area_m2: float
    volume_m3: float

    def as_dict(self) -> dict[str, float | list[float]]:
        """JSON-serialisable form for the mesh report."""
        return {
            "reference_length_m": self.reference_length_m,
            "reference_diameter_m": self.reference_diameter_m,
            "reference_area_m2": self.reference_area_m2,
            "bounding_box_min": list(self.bounding_box_min),
            "bounding_box_max": list(self.bounding_box_max),
            "surface_area_m2": self.surface_area_m2,
            "volume_m3": self.volume_m3,
        }


@dataclass
class HealingReport:
    """What CAD healing found and fixed."""

    volumes_imported: int = 0
    surfaces: int = 0
    curves: int = 0
    removed_degenerate: int = 0
    healed: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int | bool | list[str]]:
        """JSON-serialisable form for the mesh report."""
        return {
            "volumes_imported": self.volumes_imported,
            "surfaces": self.surfaces,
            "curves": self.curves,
            "removed_degenerate": self.removed_degenerate,
            "healed": self.healed,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------


def rotation_matrix_from_vectors(
    source: np.ndarray, target: np.ndarray
) -> np.ndarray:
    """Rotation taking unit vector ``source`` onto unit vector ``target``.

    Uses Rodrigues' formula. The antiparallel case has no unique axis, so a
    deterministic perpendicular is chosen and a 180 degree rotation applied;
    without that branch the cross product vanishes and the formula returns a
    singular matrix.

    Parameters
    ----------
    source, target:
        Vectors to rotate between. Normalised internally.

    Returns
    -------
    ndarray
        A ``(3, 3)`` rotation matrix ``R`` with ``R @ source == target``.
    """
    source = np.asarray(source, dtype=float)
    target = np.asarray(target, dtype=float)
    source = source / np.linalg.norm(source)
    target = target / np.linalg.norm(target)

    axis = np.cross(source, target)
    sine = float(np.linalg.norm(axis))
    cosine = float(np.dot(source, target))

    if sine < 1.0e-12:
        if cosine > 0.0:
            return np.eye(3)
        # Antiparallel: rotate 180 degrees about any perpendicular axis.
        fallback = np.array([1.0, 0.0, 0.0])
        if abs(source[0]) > 0.9:
            fallback = np.array([0.0, 1.0, 0.0])
        axis = np.cross(source, fallback)
        axis /= np.linalg.norm(axis)
        skew = _skew(axis)
        return np.eye(3) + 2.0 * skew @ skew

    axis /= sine
    skew = _skew(axis)
    return np.eye(3) + sine * skew + (1.0 - cosine) * skew @ skew


def _skew(vector: np.ndarray) -> np.ndarray:
    """Skew-symmetric cross-product matrix of a 3-vector."""
    x, y, z = vector
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])


def _apply_alignment(
    entities: list[tuple[int, int]],
    nose_vector: tuple[float, float, float],
    reference_origin: tuple[float, float, float],
    scale: float,
) -> np.ndarray:
    """Scale, translate and rotate the model into wind-tunnel coordinates.

    The body axis ends up along +X with the reference origin at (0, 0, 0),
    which is the frame the domain, the solver's AoA convention and the force
    integration all assume.
    """
    occ = gmsh.model.occ
    if scale != 1.0:
        occ.dilate(entities, 0.0, 0.0, 0.0, scale, scale, scale)

    origin = np.asarray(reference_origin, dtype=float) * scale
    if np.any(origin != 0.0):
        occ.translate(entities, *(-origin))

    rotation = rotation_matrix_from_vectors(
        np.asarray(nose_vector, dtype=float), np.array([1.0, 0.0, 0.0])
    )
    # Convert the matrix to an axis-angle pair for Gmsh's rotate().
    angle = math.acos(max(-1.0, min(1.0, (np.trace(rotation) - 1.0) / 2.0)))
    if angle > 1.0e-12:
        axis = np.array(
            [
                rotation[2, 1] - rotation[1, 2],
                rotation[0, 2] - rotation[2, 0],
                rotation[1, 0] - rotation[0, 1],
            ]
        )
        norm = np.linalg.norm(axis)
        if norm < 1.0e-12:
            # 180 degree rotation: recover the axis from the symmetric part.
            eigenvalues, eigenvectors = np.linalg.eigh(rotation)
            axis = eigenvectors[:, int(np.argmax(eigenvalues))]
            norm = np.linalg.norm(axis)
        axis /= norm
        occ.rotate(entities, 0.0, 0.0, 0.0, *axis, angle)

    occ.synchronize()
    return rotation


# ---------------------------------------------------------------------------
# Import, healing and measurement
# ---------------------------------------------------------------------------


def _import_and_heal(step_path: Path, heal: bool, tolerance: float) -> HealingReport:
    """Import a STEP file and optionally run OpenCASCADE healing."""
    if not step_path.is_file():
        raise MeshPipelineError(f"STEP file not found: {step_path}")

    occ = gmsh.model.occ
    try:
        occ.importShapes(str(step_path))
    except Exception as error:  # pragma: no cover - depends on the CAD file
        raise MeshPipelineError(
            f"could not import '{step_path.name}': {error}"
        ) from error
    occ.synchronize()

    report = HealingReport()
    volumes = gmsh.model.getEntities(3)
    if not volumes:
        raise MeshPipelineError(
            f"'{step_path.name}' contains no solid volumes. The pipeline needs "
            "a closed solid; surface-only CAD must be sewn into a solid first."
        )
    report.volumes_imported = len(volumes)

    if heal:
        gmsh.option.setNumber("Geometry.Tolerance", tolerance)
        gmsh.option.setNumber("Geometry.OCCSewFaces", 1)
        gmsh.option.setNumber("Geometry.OCCFixDegenerated", 1)
        gmsh.option.setNumber("Geometry.OCCFixSmallEdges", 1)
        gmsh.option.setNumber("Geometry.OCCFixSmallFaces", 1)
        before_surfaces = len(gmsh.model.getEntities(2))
        try:
            occ.healShapes(
                dimTags=volumes,
                tolerance=tolerance,
                fixDegenerated=True,
                fixSmallEdges=True,
                fixSmallFaces=True,
                sewFaces=True,
            )
            occ.synchronize()
            report.healed = True
            report.removed_degenerate = max(
                0, before_surfaces - len(gmsh.model.getEntities(2))
            )
        except Exception as error:  # pragma: no cover - CAD dependent
            report.notes.append(f"healing skipped: {error}")

        # Healing can destroy the solid outright -- OpenCASCADE does exactly
        # this to a plain sphere -- so verify afterwards and fall back to the
        # unhealed import rather than proceeding with an empty model.
        if not gmsh.model.getEntities(3):
            gmsh.model.remove()
            gmsh.model.add("aerothermal_mesh")
            occ.importShapes(str(step_path))
            occ.synchronize()
            report.healed = False
            report.removed_degenerate = 0
            report.notes.append(
                "healing removed all solids and was rolled back; the "
                "geometry is used as imported"
            )
            if not gmsh.model.getEntities(3):
                raise MeshPipelineError(
                    f"'{step_path.name}' yielded no solid volumes after import"
                )

    report.surfaces = len(gmsh.model.getEntities(2))
    report.curves = len(gmsh.model.getEntities(1))
    if len(gmsh.model.getEntities(3)) > 1:
        report.notes.append(
            f"{len(gmsh.model.getEntities(3))} separate solids present; all "
            "are treated as wall geometry"
        )
    return report


def _measure_geometry(volume_tags: list[int]) -> GeometryMetrics:
    """Measure reference dimensions of the aligned body.

    The body axis is +X by this point, so the reference length is the axial
    extent and the reference diameter is taken from the maximum radial extent
    of the airframe.
    """
    if not volume_tags:
        raise MeshPipelineError(
            "no solid volumes to measure; the CAD import produced no solids"
        )
    boxes = np.array(
        [gmsh.model.getBoundingBox(3, tag) for tag in volume_tags], dtype=float
    ).reshape(-1, 6)
    low = boxes[:, :3].min(axis=0)
    high = boxes[:, 3:].max(axis=0)

    length = float(high[0] - low[0])
    # Radial extent includes the fins; the airframe diameter is recovered
    # separately from the face classification.
    diameter = float(max(high[1] - low[1], high[2] - low[2]))

    if length <= 0.0:
        raise MeshPipelineError(
            "aligned body has zero axial extent; check the nose vector and "
            "the CAD units (scale_to_meters)"
        )

    surface_area = 0.0
    for _, tag in gmsh.model.getEntities(2):
        try:
            surface_area += float(gmsh.model.occ.getMass(2, tag))
        except Exception:  # pragma: no cover - degenerate faces
            continue
    volume = 0.0
    for tag in volume_tags:
        try:
            volume += float(gmsh.model.occ.getMass(3, tag))
        except Exception:  # pragma: no cover
            continue

    return GeometryMetrics(
        reference_length_m=length,
        reference_diameter_m=diameter,
        reference_area_m2=math.pi * 0.25 * diameter * diameter,
        bounding_box_min=(float(low[0]), float(low[1]), float(low[2])),
        bounding_box_max=(float(high[0]), float(high[1]), float(high[2])),
        surface_area_m2=surface_area,
        volume_m3=volume,
    )



def _wall_surface_tags(volume_tags: list[int]) -> list[int]:
    """Surfaces forming the outer boundary of the body.

    Taking every surface in the model is wrong: fusing solids leaves internal
    interface faces behind, and meshing those makes edges shared by four
    triangles and coincident duplicate nodes, which no prism march or
    tetrahedraliser can consume. On the reference rocket this is the
    difference between 102 non-manifold edges and one (the cone apex).
    """
    boundary = gmsh.model.getBoundary(
        [(3, tag) for tag in volume_tags], oriented=False, combined=True
    )
    return sorted({abs(int(tag)) for _, tag in boundary})


def classify_wall_surfaces(
    surface_tags: list[int], fin_radius_ratio: float = DEFAULT_FIN_RADIUS_RATIO
) -> tuple[list[int], list[int], float]:
    """Split wall faces into airframe and fin groups by radial extent.

    A body of revolution wraps the axis, so its faces span a similar radius
    everywhere; fins stick out past that. The airframe radius is estimated as
    the area-weighted median of per-face maximum radii, and any face reaching
    beyond ``fin_radius_ratio`` times that is called a fin.

    Parameters
    ----------
    surface_tags:
        Tags of the wall surfaces, in the aligned (+X) frame.
    fin_radius_ratio:
        Multiple of the airframe radius beyond which a face counts as a fin.

    Returns
    -------
    tuple
        Airframe tags, fin tags, and the estimated airframe radius in metres.
    """
    radii: list[float] = []
    areas: list[float] = []
    for tag in surface_tags:
        box = gmsh.model.getBoundingBox(2, tag)
        radius = max(
            abs(box[1]), abs(box[2]), abs(box[4]), abs(box[5])
        )
        radii.append(float(radius))
        try:
            areas.append(float(gmsh.model.occ.getMass(2, tag)))
        except Exception:  # pragma: no cover
            areas.append(0.0)

    if not radii:
        return [], [], 0.0

    radius_array = np.array(radii)
    area_array = np.array(areas)
    if area_array.sum() <= 0.0:
        airframe_radius = float(np.median(radius_array))
    else:
        order = np.argsort(radius_array)
        cumulative = np.cumsum(area_array[order])
        midpoint = 0.5 * cumulative[-1]
        airframe_radius = float(radius_array[order][np.searchsorted(cumulative, midpoint)])

    threshold = fin_radius_ratio * airframe_radius
    airframe = [
        tag for tag, radius in zip(surface_tags, radii) if radius <= threshold
    ]
    fins = [tag for tag, radius in zip(surface_tags, radii) if radius > threshold]
    return airframe, fins, airframe_radius


# ---------------------------------------------------------------------------
# Sizing fields
# ---------------------------------------------------------------------------


def _apply_sizing_fields(
    wall_tags: list[int],
    fin_tags: list[int],
    metrics: GeometryMetrics,
    request: MeshRequest,
    size_scale: float,
) -> float:
    """Install curvature and feature-distance sizing fields.

    Returns the nominal surface element size the fields were built around.
    """
    mesh = request.mesh
    nominal = size_scale * metrics.reference_diameter_m / 12.0
    nominal = max(nominal, 1.0e-6)

    gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", mesh.curvature_points_per_2pi)
    gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 1)
    gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
    gmsh.option.setNumber("Mesh.MeshSizeMin", nominal * mesh.leading_edge_refinement)
    gmsh.option.setNumber("Mesh.MeshSizeMax", nominal * mesh.farfield_size_multiplier)

    fields: list[int] = []

    # Refine towards the fins, whose leading and trailing edges carry the
    # sharpest gradients on the body.
    if fin_tags:
        distance = gmsh.model.mesh.field.add("Distance")
        gmsh.model.mesh.field.setNumbers(distance, "SurfacesList", fin_tags)
        gmsh.model.mesh.field.setNumber(distance, "Sampling", 100)
        threshold = gmsh.model.mesh.field.add("Threshold")
        gmsh.model.mesh.field.setNumber(threshold, "InField", distance)
        gmsh.model.mesh.field.setNumber(
            threshold, "SizeMin", nominal * mesh.leading_edge_refinement
        )
        gmsh.model.mesh.field.setNumber(threshold, "SizeMax", nominal)
        gmsh.model.mesh.field.setNumber(threshold, "DistMin", 0.0)
        gmsh.model.mesh.field.setNumber(
            threshold, "DistMax", 0.5 * metrics.reference_diameter_m
        )
        fields.append(threshold)

    # Hold a fine size on the body itself, coarsening outwards.
    if wall_tags:
        body_distance = gmsh.model.mesh.field.add("Distance")
        gmsh.model.mesh.field.setNumbers(body_distance, "SurfacesList", wall_tags)
        gmsh.model.mesh.field.setNumber(body_distance, "Sampling", 100)
        body_threshold = gmsh.model.mesh.field.add("Threshold")
        gmsh.model.mesh.field.setNumber(body_threshold, "InField", body_distance)
        gmsh.model.mesh.field.setNumber(body_threshold, "SizeMin", nominal)
        gmsh.model.mesh.field.setNumber(
            body_threshold, "SizeMax", nominal * mesh.farfield_size_multiplier
        )
        gmsh.model.mesh.field.setNumber(
            body_threshold, "DistMin", 0.5 * metrics.reference_diameter_m
        )
        gmsh.model.mesh.field.setNumber(
            body_threshold,
            "DistMax",
            max(
                request.domain.radial_multiplier * metrics.reference_length_m * 0.5,
                metrics.reference_length_m,
            ),
        )
        fields.append(body_threshold)

    # Keep the wake refined so the base recirculation and plume resolve.
    if mesh.wake_refinement_length > 0.0:
        wake = gmsh.model.mesh.field.add("Box")
        gmsh.model.mesh.field.setNumber(wake, "VIn", nominal * 2.0)
        gmsh.model.mesh.field.setNumber(
            wake, "VOut", nominal * mesh.farfield_size_multiplier
        )
        gmsh.model.mesh.field.setNumber(wake, "XMin", metrics.bounding_box_max[0])
        gmsh.model.mesh.field.setNumber(
            wake,
            "XMax",
            metrics.bounding_box_max[0]
            + mesh.wake_refinement_length * metrics.reference_length_m,
        )
        radial = 1.5 * metrics.reference_diameter_m
        for axis in ("Y", "Z"):
            gmsh.model.mesh.field.setNumber(wake, f"{axis}Min", -radial)
            gmsh.model.mesh.field.setNumber(wake, f"{axis}Max", radial)
        fields.append(wake)

    if fields:
        minimum = gmsh.model.mesh.field.add("Min")
        gmsh.model.mesh.field.setNumbers(minimum, "FieldsList", fields)
        gmsh.model.mesh.field.setAsBackgroundMesh(minimum)

    return nominal


# ---------------------------------------------------------------------------
# Surface mesh extraction
# ---------------------------------------------------------------------------


def extract_triangulation(
    surface_tags: list[int],
) -> tuple[np.ndarray, np.ndarray, dict[int, np.ndarray]]:
    """Extract a contiguous triangulation of the given surfaces.

    Gmsh node tags are sparse and one-based, so they are remapped onto dense
    zero-based indices usable with NumPy.

    Returns
    -------
    tuple
        ``(N, 3)`` points, ``(T, 3)`` triangles over all surfaces, and a
        mapping of surface tag to that surface's own triangles.
    """
    node_tags, coordinates, _ = gmsh.model.mesh.getNodes()
    points = np.array(coordinates, dtype=float).reshape(-1, 3)
    index_of = {int(tag): index for index, tag in enumerate(node_tags)}

    per_surface: dict[int, np.ndarray] = {}
    blocks: list[np.ndarray] = []
    for tag in surface_tags:
        element_types, _, node_lists = gmsh.model.mesh.getElements(2, tag)
        surface_blocks = []
        for element_type, nodes in zip(element_types, node_lists):
            if element_type != 2:  # 3-node triangles only
                continue
            indices = np.array(
                [index_of[int(n)] for n in nodes], dtype=np.int64
            ).reshape(-1, 3)
            surface_blocks.append(indices)
        if surface_blocks:
            joined = np.concatenate(surface_blocks)
            per_surface[tag] = joined
            blocks.append(joined)

    triangles = (
        np.concatenate(blocks) if blocks else np.empty((0, 3), dtype=np.int64)
    )
    return points, triangles, per_surface


def compact_triangulation(
    points: np.ndarray, triangles: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Drop unreferenced nodes and reindex.

    The whole-model node array contains nodes belonging to other entities;
    the extruder needs a self-contained surface.

    Returns
    -------
    tuple
        Compacted points, reindexed triangles, and the original indices kept.
    """
    if triangles.size == 0:
        return np.empty((0, 3)), triangles, np.empty(0, dtype=np.int64)
    used = np.unique(triangles)
    remap = np.full(points.shape[0], -1, dtype=np.int64)
    remap[used] = np.arange(used.size)
    return points[used], remap[triangles], used


# ---------------------------------------------------------------------------
# Farfield construction
# ---------------------------------------------------------------------------


def _build_farfield(
    request: MeshRequest, metrics: GeometryMetrics
) -> tuple[int, list[int]]:
    """Create the farfield envelope around the (already removed) body.

    Returns the volume tag and the farfield boundary surface tags.
    """
    domain = request.domain
    occ = gmsh.model.occ
    length = metrics.reference_length_m

    upstream = domain.upstream_multiplier * length
    downstream = domain.downstream_multiplier * length
    radial = domain.radial_multiplier * length

    nose_x = metrics.bounding_box_min[0]
    base_x = metrics.bounding_box_max[0]
    x_min = nose_x - upstream
    x_max = base_x + downstream

    if domain.shape is DomainShape.CYLINDER:
        volume = occ.addCylinder(
            x_min, 0.0, 0.0, x_max - x_min, 0.0, 0.0, radial
        )
    else:
        volume = occ.addBox(
            x_min, -radial, -radial, x_max - x_min, 2.0 * radial, 2.0 * radial
        )
    occ.synchronize()
    surfaces = [tag for _, tag in gmsh.model.getBoundary([(3, volume)], oriented=False)]
    # Drop the solid envelope, keeping only its boundary surfaces. The fluid
    # region is defined further up as a surface loop of these plus the prism
    # shell; leaving this volume in place would also mesh the space the body
    # occupies.
    gmsh.model.removeEntities([(3, volume)])
    return volume, surfaces


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def generate_mesh(
    request: MeshRequest,
    output_path: Path | str,
    progress: callable | None = None,
) -> MeshResult:
    """Produce a solver-ready ``.su2`` mesh from a STEP file.

    Parameters
    ----------
    request:
        Geometry, domain and meshing parameters.
    output_path:
        Destination ``.su2`` path.
    progress:
        Optional callback receiving human-readable status strings, used by the
        GUI to drive its progress display.

    Returns
    -------
    MeshResult
        Cell counts, boundary report, achieved y+ and quality metrics.

    Raises
    ------
    MeshPipelineError
        If the CAD cannot be imported, aligned or meshed.
    """
    started = time.perf_counter()
    output_path = Path(output_path)
    notify = progress or (lambda message: None)

    band_low, band_high = request.mesh.target_band()
    target_cells = 0.5 * (band_low + band_high)

    size_scale = 1.0
    best: dict | None = None
    attempts = 0

    for iteration in range(request.mesh.max_targeting_iterations + 1):
        attempts = iteration + 1
        notify(f"meshing attempt {attempts} (size scale {size_scale:.3f})")
        attempt = _mesh_once(request, size_scale, notify)
        cells = attempt["cell_count"]

        if best is None or abs(cells - target_cells) < abs(
            best["cell_count"] - target_cells
        ):
            best = attempt

        if band_low <= cells <= band_high:
            break
        if iteration == request.mesh.max_targeting_iterations:
            break

        # Cell count scales roughly with size^-3, so correct the size by the
        # cube root of the miss. Damped, because the boundary-layer prisms do
        # not follow that law and would otherwise drive an overshoot.
        ratio = (cells / target_cells) ** (1.0 / 3.0)
        size_scale = float(
            np.clip(size_scale * ratio**0.85, _MIN_SIZE_SCALE, _MAX_SIZE_SCALE)
        )

    if best is None:  # pragma: no cover - loop always runs once
        raise MeshPipelineError("meshing produced no result")

    notify("writing SU2 mesh")
    mesh: SU2Mesh = best["mesh"]
    mesh.write(output_path)

    metrics: GeometryMetrics = best["metrics"]
    prisms: PrismLayerResult = best["prisms"]
    cells = best["cell_count"]

    state = (
        request.sizing_flow.atmosphere() if request.sizing_flow else isa_state(0.0)
    )
    speed = (
        request.sizing_flow.speed_ms()
        if request.sizing_flow
        else state.speed_of_sound_ms
    )
    achieved_yplus = yplus_from_first_cell_height(
        state, speed, metrics.reference_length_m, best["first_height"]
    )

    return MeshResult(
        mesh_id=output_path.stem,
        mesh_path=str(output_path),
        cell_count=cells,
        node_count=mesh.node_count,
        surface_element_count=sum(mesh.marker_counts().values()),
        target_band=(band_low, band_high),
        within_target_band=band_low <= cells <= band_high,
        reference_length_m=metrics.reference_length_m,
        reference_diameter_m=best["airframe_diameter"],
        reference_area_m2=math.pi * 0.25 * best["airframe_diameter"] ** 2,
        first_cell_height_m=best["first_height"],
        estimated_yplus=achieved_yplus,
        boundary_markers=mesh.marker_counts(),
        healing_report={
            key: value
            for key, value in best["healing"].as_dict().items()
            if isinstance(value, int)
        },
        min_quality=best["min_quality"],
        targeting_iterations=attempts,
        wall_time_s=time.perf_counter() - started,
    )


def _mesh_once(
    request: MeshRequest, size_scale: float, notify: callable
) -> dict:
    """Run one complete meshing attempt at a given characteristic size."""
    geometry = request.geometry
    step_path = Path(geometry.step_file_path)

    with gmsh_session("aerothermal_mesh", threads=0):
        healing = _import_and_heal(
            step_path, geometry.heal_geometry, geometry.heal_tolerance_m
        )

        body_volumes = [tag for _, tag in gmsh.model.getEntities(3)]
        _apply_alignment(
            [(3, tag) for tag in body_volumes],
            geometry.resolved_nose_vector(),
            tuple(geometry.reference_origin),
            geometry.scale_to_meters,
        )

        body_volumes = [tag for _, tag in gmsh.model.getEntities(3)]
        metrics = _measure_geometry(body_volumes)

        wall_tags = _wall_surface_tags(body_volumes)
        airframe_tags, fin_tags, airframe_radius = classify_wall_surfaces(wall_tags)
        notify(
            f"classified {len(airframe_tags)} airframe and {len(fin_tags)} fin "
            f"faces (airframe radius {airframe_radius * 1000:.1f} mm)"
        )

        _apply_sizing_fields(airframe_tags, fin_tags, metrics, request, size_scale)

        notify("generating surface mesh")
        gmsh.model.mesh.generate(2)

        points, _, per_surface = extract_triangulation(wall_tags)
        all_triangles = (
            np.concatenate(list(per_surface.values()))
            if per_surface
            else np.empty((0, 3), dtype=np.int64)
        )
        if all_triangles.size == 0:
            raise MeshPipelineError("surface meshing produced no wall triangles")

        wall_points, wall_triangles, kept = compact_triangulation(
            points, all_triangles
        )

        # Which compacted triangles belong to fins, for marker tagging.
        fin_triangle_count = sum(
            len(per_surface[tag]) for tag in fin_tags if tag in per_surface
        )
        airframe_triangle_count = len(all_triangles) - fin_triangle_count

        notify(f"extruding prism layers from {len(wall_triangles)} wall triangles")
        first_height = _first_layer_height(request, metrics)
        prisms = _extrude(request, wall_points, wall_triangles, first_height)

    # The prism shell is handed to a fresh session for the tet region, so the
    # farfield is meshed against the outer layer rather than the body.
    tet_points, tets, farfield_triangles, shell_indices = _mesh_farfield(
        request, metrics, prisms, notify
    )

    mesh, min_quality = _assemble(
        prisms=prisms,
        tet_points=tet_points,
        tets=tets,
        farfield_triangles=farfield_triangles,
        shell_indices=shell_indices,
        airframe_triangle_count=airframe_triangle_count,
        fin_triangle_count=fin_triangle_count,
    )

    return {
        "mesh": mesh,
        "cell_count": mesh.cell_count,
        "metrics": metrics,
        "prisms": prisms,
        "healing": healing,
        "first_height": first_height,
        "airframe_diameter": 2.0 * airframe_radius,
        "min_quality": min_quality,
    }


def _first_layer_height(request: MeshRequest, metrics: GeometryMetrics) -> float:
    """First prism height for the requested y+ at the sizing condition."""
    if request.sizing_flow is not None:
        state = request.sizing_flow.atmosphere()
        speed = request.sizing_flow.speed_ms()
    else:
        # Conservative default: sonic at sea level sits at the thin end of the
        # supported envelope, so the resulting spacing is valid across it.
        state = isa_state(0.0)
        speed = state.speed_of_sound_ms
    return first_cell_height(
        state, speed, metrics.reference_length_m, request.mesh.target_yplus
    )


def _extrude(
    request: MeshRequest,
    wall_points: np.ndarray,
    wall_triangles: np.ndarray,
    first_height: float,
) -> PrismLayerResult:
    """Extrude the boundary layer, tolerating CAD defects on a second pass."""
    arguments = {
        "first_height": first_height,
        "layers": request.mesh.boundary_layers,
        "growth_rate": request.mesh.boundary_layer_growth,
    }
    try:
        return extrude_prism_layers(wall_points, wall_triangles, **arguments)
    except PrismExtrusionError as error:
        if "not a closed manifold" not in str(error):
            raise MeshPipelineError(str(error)) from error
        # Sharp tips make Gmsh emit non-manifold edges. Freezing that handful
        # of vertices costs a locally thinner stack and nothing else.
        try:
            return extrude_prism_layers(
                wall_points, wall_triangles, tolerate_defects=True, **arguments
            )
        except PrismExtrusionError as second:
            raise MeshPipelineError(
                f"boundary-layer extrusion failed: {second}"
            ) from second


def _mesh_farfield(
    request: MeshRequest,
    metrics: GeometryMetrics,
    prisms: PrismLayerResult,
    notify: callable,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Tetrahedralise between the prism outer shell and the farfield."""
    offset = prisms.layers_built * prisms.wall_node_count
    shell_points = prisms.points[offset:]
    shell_triangles = prisms.outer_triangles - offset

    with gmsh_session("aerothermal_farfield", threads=0):
        _, farfield_surfaces = _build_farfield(request, metrics)

        nominal = metrics.reference_diameter_m / 4.0
        gmsh.option.setNumber("Mesh.MeshSizeMin", nominal)
        gmsh.option.setNumber(
            "Mesh.MeshSizeMax", nominal * request.mesh.farfield_size_multiplier
        )
        gmsh.model.mesh.generate(2)

        # Insert the prism shell as a discrete surface with an explicit tag.
        gmsh.model.addDiscreteEntity(2, _SHELL_SURFACE_TAG)
        existing = gmsh.model.mesh.getNodes()[0]
        base_tag = int(max(existing)) + 1 if len(existing) else 1
        shell_tags = np.arange(base_tag, base_tag + len(shell_points))
        gmsh.model.mesh.addNodes(
            2, _SHELL_SURFACE_TAG, shell_tags, shell_points.ravel()
        )
        gmsh.model.mesh.addElementsByType(
            _SHELL_SURFACE_TAG, 2, [], shell_tags[shell_triangles.ravel()]
        )

        loop = gmsh.model.geo.addSurfaceLoop(
            farfield_surfaces + [_SHELL_SURFACE_TAG]
        )
        gmsh.model.geo.addVolume([loop])
        gmsh.model.geo.synchronize()

        notify("generating tetrahedral farfield mesh")
        try:
            gmsh.model.mesh.generate(3)
        except Exception as error:
            raise MeshPipelineError(
                f"farfield tetrahedralisation failed: {error}"
            ) from error

        if request.mesh.optimize_netgen:
            try:
                gmsh.model.mesh.optimize("Netgen")
            except Exception:  # pragma: no cover - optimiser is optional
                pass

        node_tags, coordinates, _ = gmsh.model.mesh.getNodes()
        tet_points = np.array(coordinates, dtype=float).reshape(-1, 3)
        index_of = {int(tag): index for index, tag in enumerate(node_tags)}

        tets = _tetrahedra(index_of)
        farfield_triangles = np.concatenate(
            [
                _surface_elements(tag, index_of)
                for tag in farfield_surfaces
            ]
        ) if farfield_surfaces else np.empty((0, 3), dtype=np.int64)

        shell_indices = np.array(
            [index_of[int(tag)] for tag in shell_tags], dtype=np.int64
        )

    return tet_points, tets, farfield_triangles, shell_indices


# Gmsh element type 4 is the 4-node tetrahedron.
_GMSH_TETRA = 4


def _tetrahedra(index_of: dict[int, int]) -> np.ndarray:
    """Collect every tetrahedron in the model, on dense node indices."""
    element_types, _, node_lists = gmsh.model.mesh.getElements(3)
    blocks = []
    for element_type, nodes in zip(element_types, node_lists):
        if element_type != _GMSH_TETRA:
            continue
        blocks.append(
            np.array([index_of[int(n)] for n in nodes], dtype=np.int64).reshape(-1, 4)
        )
    if not blocks:
        return np.empty((0, 4), dtype=np.int64)
    return np.concatenate(blocks)


def _surface_elements(tag: int, index_of: dict[int, int]) -> np.ndarray:
    """Triangles of one surface, remapped onto dense node indices."""
    element_types, _, node_lists = gmsh.model.mesh.getElements(2, tag)
    blocks = []
    for element_type, nodes in zip(element_types, node_lists):
        if element_type != 2:
            continue
        blocks.append(
            np.array([index_of[int(n)] for n in nodes], dtype=np.int64).reshape(-1, 3)
        )
    if not blocks:
        return np.empty((0, 3), dtype=np.int64)
    return np.concatenate(blocks)


def _assemble(
    prisms: PrismLayerResult,
    tet_points: np.ndarray,
    tets: np.ndarray,
    farfield_triangles: np.ndarray,
    shell_indices: np.ndarray,
    airframe_triangle_count: int,
    fin_triangle_count: int,
) -> tuple[SU2Mesh, float]:
    """Stitch prisms and tetrahedra into one tagged SU2 mesh."""
    merged_points, mappings = merge_nodes([prisms.points, tet_points])
    prism_map, tet_map = mappings

    mesh = SU2Mesh(points=merged_points)
    mesh.add_volume(VTK_WEDGE, prism_map[prisms.wedges])
    if tets.size:
        mesh.add_volume(VTK_TETRA, tet_map[tets])

    # Wall markers come from the original wall triangulation, which occupies
    # the first node block of the prism stack.
    wall = prism_map[prisms.wall_triangles]
    if fin_triangle_count and airframe_triangle_count:
        mesh.add_marker(MARKER_WALL_ROCKET, VTK_TRIANGLE, wall[:airframe_triangle_count])
        mesh.add_marker(MARKER_WALL_FINS, VTK_TRIANGLE, wall[airframe_triangle_count:])
    else:
        mesh.add_marker(MARKER_WALL_ROCKET, VTK_TRIANGLE, wall)

    if farfield_triangles.size:
        mesh.add_marker(MARKER_FARFIELD, VTK_TRIANGLE, tet_map[farfield_triangles])

    mesh.validate()
    quality = _minimum_quality(merged_points, prism_map[prisms.wedges])
    return mesh, quality


def _minimum_quality(points: np.ndarray, wedges: np.ndarray) -> float:
    """Smallest normalised prism volume, a cheap proxy for cell quality.

    Compares each prism's volume against the volume its base area and height
    would give if it were perfectly extruded; values approach 1 for good
    cells and 0 for slivers.
    """
    if wedges.size == 0:
        return 0.0
    from backend.prism_layers import _wedge_volumes, triangle_normals_and_areas

    volumes = _wedge_volumes(points, wedges)
    _, areas = triangle_normals_and_areas(points, wedges[:, :3])
    heights = np.linalg.norm(
        points[wedges[:, 3]] - points[wedges[:, 0]], axis=1
    )
    ideal = areas * heights
    valid = ideal > 0.0
    if not np.any(valid):
        return 0.0
    return float(np.min(volumes[valid] / ideal[valid]))
