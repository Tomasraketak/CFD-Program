"""SU2 design points for the SPS30 housing study, with droplet tracking.

1. **Geometry** -- the housing STEP (or the built-in one from
   :func:`create_sps30_housing_step`) is turned so the platform travels
   towards -x (wind along +x, z up) and placed in a wind tunnel sized in
   housing lengths.
2. **Mesh** -- the housing is cut out of the tunnel; its internal plenum,
   baffle and sensor chamber stay connected to the outside air through the
   side slits and the weep hole. Faces: INLET, OUTLET, SIDE_NEG, SIDE_POS
   (y = min/max), TOP, BOTTOM, SENSOR (the SPS30 intake face) and HOUSING.
3. **Air** -- SU2 incompressible RANS with SST k-omega (as the
   specification requires). Yaw turns the inlet velocity about z; the
   windward side face becomes a velocity inlet too and the leeward one an
   outlet, so a crosswind needs no remeshing. The SPS30 fan is a small
   prescribed suction velocity through the sensor face.
4. **Droplets** -- SU2 has no discrete phase model, so droplets are tracked
   here through the solved air field, one-way coupled: Schiller-Naumann
   drag, gravity, and a discrete random walk from the SST k and omega
   (Fluent's DPM stochastic tracking). A droplet stops where it meets a
   wall ("trap"); one that reaches the SENSOR face is a failure.
5. **Outputs** -- the maximum air speed 2 mm in front of the sensor face,
   the share of droplets that got inside the housing and reached the face,
   and the exchange flow across a plane through the sensor chamber.
"""

from __future__ import annotations

import json
import math
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from core.sps30_models import Sps30CfdSettings, Sps30Point, Sps30Setup

MARKERS = ("INLET", "OUTLET", "SIDE_NEG", "SIDE_POS", "TOP", "BOTTOM", "SENSOR", "HOUSING")
MESH_FILENAME = "sps30_domain.su2"
GEOMETRY_FILENAME = "geometry.json"

# Surface size on the housing and in the far field, metres.
RESOLUTION_SIZES = {
    "coarse": (0.002, 0.05),
    "medium": (0.0015, 0.035),
    "fine": (0.001, 0.025),
}


class Sps30CfdError(RuntimeError):
    """The SPS30 CFD case could not be built, run or read."""


# ---------------------------------------------------------------------------
# The built-in housing
# ---------------------------------------------------------------------------


def create_sps30_housing_step(output_path: Path | str) -> Path:
    """A 120 x 70 x 80 mm housing: side slits, plenum, baffle, sensor chamber.

    The platform travels towards -x. Two 20 x 3 mm slits in the side walls
    (y = +/-35 mm) near the front open into the plenum; a baffle across the
    housing at x = 58-61 mm rises from the floor to 15 mm below the roof,
    so air reaches the sensor chamber behind it only by turning over its
    top edge. The SPS30 intake is the -x face of a 20 x 20 mm stub on the
    back wall, centred at (107, 0, 30) mm. A 4 mm weep hole drains the
    plenum floor at x = 45 mm.
    """
    import gmsh

    from backend.gmsh_session import gmsh_session

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    L, W, H, t = 0.12, 0.07, 0.08, 0.003
    with gmsh_session("sps30_housing"):
        occ = gmsh.model.occ
        outer = occ.addBox(0.0, -W / 2, 0.0, L, W, H)
        inner = occ.addBox(t, -W / 2 + t, t, L - 2 * t, W - 2 * t, H - 2 * t)
        shell, _ = occ.cut([(3, outer)], [(3, inner)])
        baffle = occ.addBox(0.058, -W / 2 + t, t, 0.003, W - 2 * t, H - 2 * t - 0.015)
        stub = occ.addBox(0.107, -0.01, 0.02, L - t - 0.107, 0.02, 0.02)
        body, _ = occ.fuse(shell, [(3, baffle), (3, stub)])
        slit = occ.addBox(0.015, -W, 0.045, 0.02, 2 * W, 0.003)
        weep = occ.addCylinder(0.045, 0.0, -0.001, 0.0, 0.0, t + 0.002, 0.002)
        occ.cut(body, [(3, slit), (3, weep)])
        occ.synchronize()
        gmsh.write(str(output_path))
    return output_path


# ---------------------------------------------------------------------------
# Orientation: platform travelling towards -x
# ---------------------------------------------------------------------------

_AXES = {"+x": (1, 0, 0), "-x": (-1, 0, 0), "+y": (0, 1, 0), "-y": (0, -1, 0)}


def travel_rotation(forward_axis: str) -> np.ndarray:
    """Rotation about z taking the travel direction onto -x."""
    if forward_axis not in _AXES:
        raise Sps30CfdError("forward_axis must be +x, -x, +y or -y (z stays up)")
    fx, fy, _ = _AXES[forward_axis]
    angle = math.atan2(fy, fx)  # current heading
    turn = math.pi - angle  # heading of -x is pi
    c, s = math.cos(turn), math.sin(turn)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _normal_vector(label: str) -> np.ndarray:
    vector = np.zeros(3)
    vector["xyz".index(label[1])] = 1.0 if label[0] == "+" else -1.0
    return vector


@dataclass
class Sps30Domain:
    """Where things are in the mesh frame, and what meshing produced."""

    housing_low: list[float]
    housing_high: list[float]
    sensor_center: list[float]
    sensor_normal: list[float]
    sensor_size: list[float]
    chamber_plane_x: float
    reference_length: float
    cell_count: int
    marker_counts: dict[str, int]

    def save(self, folder: Path) -> None:
        (folder / GEOMETRY_FILENAME).write_text(json.dumps(self.__dict__, indent=2))

    @classmethod
    def load(cls, folder: Path) -> "Sps30Domain":
        return cls(**json.loads((folder / GEOMETRY_FILENAME).read_text()))


def resolve_housing_step(setup: Sps30Setup, folder: Path) -> tuple[Path, float]:
    if setup.housing_step_path:
        path = Path(setup.housing_step_path)
        if not path.is_file():
            raise Sps30CfdError(f"housing STEP not found: {path}")
        if setup.scale_to_meters is not None:
            return path, setup.scale_to_meters
        from core.step_inspect import suggest_scale_to_meters

        return path, suggest_scale_to_meters(path).scale
    return create_sps30_housing_step(folder / "sps30_housing.step"), 1.0


def _place(occ, gmsh, setup: Sps30Setup, folder: Path):
    """Import, scale and turn the housing; returns solids, rotation."""
    step, scale = resolve_housing_step(setup, folder)
    solids = [e for e in occ.importShapes(str(step)) if e[0] == 3]
    if not solids:
        raise Sps30CfdError(f"{step.name} contains no solid; export the housing as a solid")
    if scale != 1.0:
        occ.dilate(solids, 0.0, 0.0, 0.0, scale, scale, scale)
    rotation = travel_rotation(setup.forward_axis)
    angle = math.atan2(rotation[1, 0], rotation[0, 0])
    if abs(angle) > 1e-12:
        occ.rotate(solids, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, angle)
    occ.synchronize()
    return solids, rotation


def mesh_sps30_domain(
    setup: Sps30Setup, output_dir: Path | str, resolution: str = "coarse", threads: int = 4
) -> Sps30Domain:
    """Cut the housing out of the wind tunnel and mesh the air."""
    import gmsh

    from backend.gmsh_session import gmsh_session
    from backend.shield_cfd import _positive_tets
    from backend.su2_mesh import VTK_TETRA, VTK_TRIANGLE, SU2Mesh

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    near, far = RESOLUTION_SIZES[resolution]
    with gmsh_session("sps30_domain", threads=threads):
        occ = gmsh.model.occ
        solids, rotation = _place(occ, gmsh, setup, output_dir)
        low = np.full(3, np.inf)
        high = np.full(3, -np.inf)
        for dim, tag in solids:
            box = gmsh.model.getBoundingBox(dim, tag)
            low, high = np.minimum(low, box[:3]), np.maximum(high, box[3:])
        size = high - low
        ref = float(max(size))
        m = setup.domain_multipliers
        d_low = low - np.array([m.get("upstream", 3.0), m.get("lateral", 3.0), m.get("vertical", 3.0)]) * ref
        d_high = high + np.array([m.get("downstream", 6.0), m.get("lateral", 3.0), m.get("vertical", 3.0)]) * ref
        box = occ.addBox(*d_low, *(d_high - d_low))
        fluid, _ = occ.cut([(3, box)], solids)
        occ.synchronize()
        if len(fluid) != 1:
            raise Sps30CfdError(f"cutting the housing out left {len(fluid)} air volumes")
        volume = fluid[0][1]

        sensor_center = rotation @ np.array(setup.sensor_face_center_m) * 1.0
        normal = rotation @ _normal_vector(setup.sensor_face_normal)
        half = 0.5 * max(setup.sensor_face_size_m) + 1e-4
        eps = 1e-5
        groups = {name: [] for name in MARKERS}
        for _, surface in gmsh.model.getBoundary(fluid, oriented=False):
            bb = np.array(gmsh.model.getBoundingBox(2, surface))
            lo, hi = bb[:3], bb[3:]
            if hi[0] < d_low[0] + eps:
                groups["INLET"].append(surface)
            elif lo[0] > d_high[0] - eps:
                groups["OUTLET"].append(surface)
            elif hi[1] < d_low[1] + eps:
                groups["SIDE_NEG"].append(surface)
            elif lo[1] > d_high[1] - eps:
                groups["SIDE_POS"].append(surface)
            elif hi[2] < d_low[2] + eps:
                groups["BOTTOM"].append(surface)
            elif lo[2] > d_high[2] - eps:
                groups["TOP"].append(surface)
            else:
                axis = int(np.argmax(np.abs(normal)))
                flat = hi[axis] - lo[axis] < 1e-6 and abs(lo[axis] - sensor_center[axis]) < 5e-4
                inside = np.all(lo - 1e-4 <= sensor_center + half) and np.all(hi + 1e-4 >= sensor_center - half)
                within = all(
                    lo[i] >= sensor_center[i] - half and hi[i] <= sensor_center[i] + half
                    for i in range(3) if i != axis
                )
                groups["SENSOR" if flat and inside and within else "HOUSING"].append(surface)
        if not groups["SENSOR"]:
            raise Sps30CfdError(
                "no housing face matches the sensor face (centre "
                f"{setup.sensor_face_center_m}, normal {setup.sensor_face_normal}, size "
                f"{setup.sensor_face_size_m}); model the SPS30 intake as its own flat face"
            )
        empty = [name for name, tags in groups.items() if not tags]
        if empty:
            raise Sps30CfdError(f"no faces found for {', '.join(empty)}")

        field = gmsh.model.mesh.field
        dist = field.add("Distance")
        field.setNumbers(dist, "SurfacesList", groups["HOUSING"] + groups["SENSOR"])
        field.setNumber(dist, "Sampling", 20)
        thr = field.add("Threshold")
        field.setNumber(thr, "InField", dist)
        field.setNumber(thr, "SizeMin", near)
        field.setNumber(thr, "SizeMax", far)
        field.setNumber(thr, "DistMin", 3 * near)
        field.setNumber(thr, "DistMax", 1.5 * ref)
        inner = field.add("Box")
        field.setNumber(inner, "VIn", near * 1.5)
        field.setNumber(inner, "VOut", far)
        for key, value in zip(("XMin", "YMin", "ZMin"), low):
            field.setNumber(inner, key, float(value))
        for key, value in zip(("XMax", "YMax", "ZMax"), high):
            field.setNumber(inner, key, float(value))
        wake = field.add("Box")
        field.setNumber(wake, "VIn", 4 * near)
        field.setNumber(wake, "VOut", far)
        field.setNumber(wake, "XMin", float(low[0] - 0.3 * ref))
        field.setNumber(wake, "XMax", float(high[0] + 2.0 * ref))
        field.setNumber(wake, "YMin", float(low[1] - 0.6 * ref))
        field.setNumber(wake, "YMax", float(high[1] + 0.6 * ref))
        field.setNumber(wake, "ZMin", float(low[2] - 0.4 * ref))
        field.setNumber(wake, "ZMax", float(high[2] + 0.4 * ref))
        combined = field.add("Min")
        field.setNumbers(combined, "FieldsList", [thr, inner, wake])
        field.setAsBackgroundMesh(combined)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 12)
        gmsh.option.setNumber("Mesh.MeshSizeMin", 0.4 * near)
        gmsh.option.setNumber("Mesh.MeshSizeMax", far)
        gmsh.option.setNumber("Mesh.Algorithm", 6)
        gmsh.option.setNumber("Mesh.Optimize", 1)
        failures = []
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
            raise Sps30CfdError("meshing the air round the housing failed: " + "; ".join(failures))

        tags, coords, _ = gmsh.model.mesh.getNodes()
        index_of = np.full(int(tags.max()) + 1, -1, dtype=np.int64)
        index_of[tags.astype(np.int64)] = np.arange(len(tags))
        points = coords.reshape(-1, 3)

        def elements(dim: int, entity: int, kind: int, width: int) -> np.ndarray:
            blocks = []
            types, _, nodes = gmsh.model.mesh.getElements(dim, entity)
            for element_type, element_nodes in zip(types, nodes):
                if element_type == kind:
                    blocks.append(index_of[element_nodes.astype(np.int64)].reshape(-1, width))
            return np.vstack(blocks) if blocks else np.empty((0, width), dtype=np.int64)

        tets = elements(3, volume, 4, 4)
        faces = {name: np.vstack([elements(2, s, 2, 3) for s in tags_]) for name, tags_ in groups.items()}

    mesh = SU2Mesh(points=points)
    mesh.add_volume(VTK_TETRA, _positive_tets(points, tets))
    for name in MARKERS:
        mesh.add_marker(name, VTK_TRIANGLE, faces[name])
    mesh.write(output_dir / MESH_FILENAME)
    np.savez(
        output_dir / "walls.npz",
        points=points,
        sensor=faces["SENSOR"],
        housing=faces["HOUSING"],
    )
    rotated_plane = (rotation @ np.array([setup.chamber_plane_x_m, 0.0, 0.0]))[0]
    domain = Sps30Domain(
        housing_low=low.tolist(),
        housing_high=high.tolist(),
        sensor_center=sensor_center.tolist(),
        sensor_normal=normal.tolist(),
        sensor_size=list(setup.sensor_face_size_m),
        chamber_plane_x=float(rotated_plane) if abs(rotation[0, 0]) > 0.5 else float(setup.chamber_plane_x_m),
        reference_length=ref,
        cell_count=int(len(tets)),
        marker_counts=mesh.marker_counts(),
    )
    domain.save(output_dir)
    return domain


# ---------------------------------------------------------------------------
# SU2 configuration
# ---------------------------------------------------------------------------


def build_sps30_config(
    setup: Sps30Setup,
    cfd: Sps30CfdSettings,
    point: Sps30Point,
    mesh_filename: str = MESH_FILENAME,
    domain_normal: Sequence[float] | None = None,
) -> str:
    from backend.sps30_study import air_properties

    rho, _mu = air_properties(setup.ambient_temp_c)
    temp = setup.ambient_temp_c + 273.15
    yaw = math.radians(point.yaw_deg)
    direction = (math.cos(yaw), math.sin(yaw), 0.0)
    speed = point.speed_ms
    inlets = ["INLET"]
    outlets = ["OUTLET"]
    symmetry = ["TOP", "BOTTOM"]
    if abs(point.yaw_deg) < 1e-9:
        symmetry += ["SIDE_NEG", "SIDE_POS"]
    elif point.yaw_deg > 0:  # flow towards +y: -y side is windward
        inlets.append("SIDE_NEG")
        outlets.append("SIDE_POS")
    else:
        inlets.append("SIDE_POS")
        outlets.append("SIDE_NEG")
    lines = [
        "% ---- AeroThermalStudio - SPS30 housing design point ----",
        f"% {point.name}: speed {speed:g} m/s, yaw {point.yaw_deg:g} deg, droplet {point.droplet_um:g} um",
        "SOLVER= INC_RANS",
        "KIND_TURB_MODEL= SST",
        "MATH_PROBLEM= DIRECT",
        "RESTART_SOL= NO",
        "INC_ENERGY_EQUATION= NO",
        "INC_DENSITY_MODEL= CONSTANT",
        "FLUID_MODEL= CONSTANT_DENSITY",
        f"INC_DENSITY_INIT= {rho:.6f}",
        f"INC_VELOCITY_INIT= ( {speed * direction[0]:.6f}, {speed * direction[1]:.6f}, 0.0 )",
        f"INC_TEMPERATURE_INIT= {temp:.4f}",
        "INC_NONDIM= DIMENSIONAL",
        "VISCOSITY_MODEL= CONSTANT_VISCOSITY",
        f"MU_CONSTANT= {_mu:.6e}",
        "FREESTREAM_TURBULENCEINTENSITY= 0.05",
        "FREESTREAM_TURB2LAMVISCRATIO= 10.0",
    ]
    inlet_entries = [
        f"{m}, {temp:.4f}, {speed:.6f}, {direction[0]:.8f}, {direction[1]:.8f}, 0.0" for m in inlets
    ]
    walls = ["HOUSING"]
    if setup.fan_enabled:
        # The fan as a prescribed suction velocity through the face, pointing
        # into the sensor. A mass-flow outlet reaches its target only by
        # slowly tuning its pressure and pulled ~500x the fan flow after
        # 500 iterations; a fixed velocity is exact from the first one.
        if domain_normal is None:
            raise Sps30CfdError("the sensor face normal is needed to model the fan")
        into = -np.asarray(domain_normal, dtype=float)
        suction = setup.fan_flow_m3s() / setup.face_area_m2()
        inlet_entries.append(
            f"SENSOR, {temp:.4f}, {suction:.8f}, {into[0]:.8f}, {into[1]:.8f}, {into[2]:.8f}"
        )
    else:
        walls.append("SENSOR")
    lines += [
        "INC_INLET_TYPE= " + ", ".join(["VELOCITY_INLET"] * len(inlet_entries)),
        "MARKER_INLET= ( " + ", ".join(inlet_entries) + " )",
    ]
    outlet_types = ["PRESSURE_OUTLET"] * len(outlets)
    outlet_values = [f"{m}, 0.0" for m in outlets]
    lines += [
        "INC_OUTLET_TYPE= " + ", ".join(outlet_types),
        "MARKER_OUTLET= ( " + ", ".join(outlet_values) + " )",
        "MARKER_HEATFLUX= ( " + ", ".join(f"{w}, 0.0" for w in walls) + " )",
        "MARKER_SYM= ( " + ", ".join(symmetry) + " )",
        "MARKER_PLOTTING= ( " + ", ".join(walls) + " )",
        "MARKER_MONITORING= ( " + ", ".join(walls) + " )",
        "NUM_METHOD_GRAD= GREEN_GAUSS",
        "CONV_NUM_METHOD_FLOW= FDS",
        "MUSCL_FLOW= YES",
        "SLOPE_LIMITER_FLOW= VENKATAKRISHNAN",
        "VENKAT_LIMITER_COEFF= 0.05",
        "CONV_NUM_METHOD_TURB= SCALAR_UPWIND",
        "MUSCL_TURB= NO",
        "TIME_DISCRE_FLOW= EULER_IMPLICIT",
        "TIME_DISCRE_TURB= EULER_IMPLICIT",
        "CFL_NUMBER= 5.0",
        "CFL_ADAPT= YES",
        "CFL_ADAPT_PARAM= ( 0.5, 1.05, 1.0, 100.0 )",
        "LINEAR_SOLVER= FGMRES",
        "LINEAR_SOLVER_PREC= ILU",
        "LINEAR_SOLVER_ERROR= 1E-4",
        "LINEAR_SOLVER_ITER= 20",
        f"ITER= {cfd.iterations}",
        "CONV_FIELD= REL_RMS_PRESSURE",
        "CONV_RESIDUAL_MINVAL= -4",
        "CONV_STARTITER= 100",
        f"MESH_FILENAME= {mesh_filename}",
        "MESH_FORMAT= SU2",
        "TABULAR_FORMAT= CSV",
        "CONV_FILENAME= history",
        "VOLUME_FILENAME= flow",
        "SURFACE_FILENAME= surface_flow",
        "OUTPUT_FILES= ( RESTART, PARAVIEW )",
        "VOLUME_OUTPUT= ( SOLUTION, PRIMITIVE )",
        "OUTPUT_WRT_FREQ= 500",
        "SCREEN_OUTPUT= ( INNER_ITER, RMS_PRESSURE, RMS_VELOCITY-X, RMS_TKE )",
        "SCREEN_WRT_FREQ_INNER= 10",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Reading the air field
# ---------------------------------------------------------------------------


def _velocity_name(dataset) -> str:
    for name in ("Velocity", "velocity"):
        if name in dataset.point_data:
            return name
    raise Sps30CfdError(f"no velocity in the solution (arrays: {list(dataset.point_data)})")


def face_velocity(dataset, domain: Sps30Domain, offset: float = 0.002) -> float:
    """Largest air speed on a grid 2 mm in front of the sensor face."""
    import pyvista as pv

    normal = np.array(domain.sensor_normal)
    centre = np.array(domain.sensor_center) + normal * offset
    helper = np.array([0.0, 0.0, 1.0]) if abs(normal[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    u = np.cross(normal, helper)
    u /= np.linalg.norm(u)
    v = np.cross(normal, u)
    a, b = domain.sensor_size
    grid = np.array(
        [centre + s * 0.45 * a * u + t * 0.45 * b * v for s in np.linspace(-1, 1, 9) for t in np.linspace(-1, 1, 9)]
    )
    sampled = pv.PolyData(grid).sample(dataset)
    valid = np.asarray(sampled.point_data["vtkValidPointMask"]).astype(bool)
    speed = np.linalg.norm(np.asarray(sampled.point_data[_velocity_name(dataset)]), axis=1)
    if not valid.any():
        raise Sps30CfdError("the points in front of the sensor face are outside the air")
    return float(speed[valid].max())


def exchange_flow_lpm(dataset, domain: Sps30Domain) -> float:
    """Half the |u_x| flux through the housing's cross-section at the chamber plane."""
    lo, hi = np.array(domain.housing_low), np.array(domain.housing_high)
    x = domain.chamber_plane_x
    ny, nz = 60, 60
    ys = np.linspace(lo[1], hi[1], ny)
    zs = np.linspace(lo[2], hi[2], nz)
    import pyvista as pv

    grid = np.array([[x, y, z] for y in ys for z in zs])
    sampled = pv.PolyData(grid).sample(dataset)
    valid = np.asarray(sampled.point_data["vtkValidPointMask"]).astype(bool)
    ux = np.asarray(sampled.point_data[_velocity_name(dataset)])[:, 0]
    cell = (ys[1] - ys[0]) * (zs[1] - zs[0])
    return float(0.5 * np.sum(np.abs(ux[valid])) * cell * 60000.0)


# ---------------------------------------------------------------------------
# Droplet tracking
# ---------------------------------------------------------------------------


@dataclass
class DropletResult:
    injected: int
    entered: int
    sensor_hits: int
    trapped_housing: int
    escaped: int
    unresolved: int

    @property
    def penetration(self) -> float:
        return self.sensor_hits / self.entered if self.entered else 0.0


class _Field:
    """Fast repeated interpolation in the solved air field."""

    def __init__(self, dataset) -> None:
        import vtk

        self.dataset = dataset
        self.velocity = np.asarray(dataset.point_data[_velocity_name(dataset)], dtype=float)
        k = dataset.point_data.get("Turb_Kin_Energy")
        w = dataset.point_data.get("Omega")
        self.k = np.asarray(k, dtype=float) if k is not None else None
        self.omega = np.asarray(w, dtype=float) if w is not None else None
        self.locator = vtk.vtkStaticCellLocator()
        self.locator.SetDataSet(dataset)
        self.locator.BuildLocator()
        self.points = np.asarray(dataset.points)

    def sample(self, positions: np.ndarray):
        """Velocity, k, omega and validity at each position (tetra barycentric)."""
        count = len(positions)
        vel = np.zeros((count, 3))
        k = np.zeros(count)
        omega = np.ones(count)
        valid = np.zeros(count, dtype=bool)
        weights = [0.0] * 8
        pcoords = [0.0, 0.0, 0.0]
        import vtk

        cell = vtk.vtkGenericCell()
        for i, p in enumerate(positions):
            cid = self.locator.FindCell(p, 1e-10, cell, pcoords, weights)
            if cid < 0:
                continue
            ids = [cell.GetPointId(j) for j in range(cell.GetNumberOfPoints())]
            w = np.array(weights[: len(ids)])
            vel[i] = w @ self.velocity[ids]
            if self.k is not None:
                k[i] = max(float(w @ self.k[ids]), 0.0)
                omega[i] = max(float(w @ self.omega[ids]), 1e-6)
            valid[i] = True
        return vel, k, omega, valid


def track_droplets(
    dataset,
    domain: Sps30Domain,
    setup: Sps30Setup,
    point: Sps30Point,
    count: int,
    random_walk: bool = True,
    seed: int = 1,
    max_time_s: float = 3.0,
    walls: dict | None = None,
) -> DropletResult:
    """Track ``count`` droplets of the point's diameter through the air field."""
    from scipy.spatial import cKDTree

    from backend.sps30_study import air_properties, relaxation_time

    rng = np.random.default_rng(seed)
    air, mu = air_properties(setup.ambient_temp_c)
    water = setup.water_density_kg_m3
    d = point.droplet_um * 1e-6
    tau0 = relaxation_time(d, water, mu)
    gravity = np.array([0.0, 0.0, -9.81])
    lo, hi = np.array(domain.housing_low), np.array(domain.housing_high)
    ref = domain.reference_length
    yaw = math.radians(point.yaw_deg)
    wind = point.speed_ms * np.array([math.cos(yaw), math.sin(yaw), 0.0])

    # Inject on a plane half a housing length upstream, over the housing's
    # frontal area enlarged by 20 %, shifted so the yawed stream carries
    # the droplets onto the housing.
    x0 = lo[0] - 0.5 * ref
    span = (hi - lo) * 1.2
    centre = 0.5 * (lo + hi)
    positions = np.column_stack([
        np.full(count, x0),
        centre[1] + (rng.random(count) - 0.5) * span[1] - math.tan(yaw) * (centre[0] - x0),
        centre[2] + (rng.random(count) - 0.5) * span[2],
    ])
    velocity = np.tile(wind, (count, 1))
    field = _Field(dataset)

    wall_points = walls["points"]
    labels = np.concatenate([np.ones(len(walls["sensor"]), dtype=int), np.zeros(len(walls["housing"]), dtype=int)])
    centroids = np.vstack([wall_points[walls["sensor"]].mean(axis=1), wall_points[walls["housing"]].mean(axis=1)])
    tree = cKDTree(centroids)

    state = np.zeros(count, dtype=int)  # 0 active, 1 sensor, 2 housing, 3 escaped
    entered = np.zeros(count, dtype=bool)
    fluct = np.zeros((count, 3))
    eddy_left = np.zeros(count)
    elapsed = np.zeros(count)
    near = RESOLUTION_SIZES["fine"][0]

    def inside_housing(p):
        return np.all((p >= lo) & (p <= hi), axis=1)

    for _ in range(40000):
        active = np.flatnonzero(state == 0)
        if not len(active):
            break
        p = positions[active]
        u, k, omega, valid = field.sample(p)
        # Leaving the air: through a wall (trap) or out of the tunnel.
        lost = active[~valid]
        if len(lost):
            _, nearest = tree.query(positions[lost])
            dist = np.linalg.norm(positions[lost] - centroids[nearest], axis=1)
            at_wall = dist < 0.25 * ref
            # Only a droplet that got inside the housing can reach the face.
            sensor = (labels[nearest] == 1) & entered[lost]
            state[lost] = np.where(at_wall, np.where(sensor, 1, 2), 3)
        keep = active[valid]
        if not len(keep):
            continue
        u, k, omega = u[valid], k[valid], omega[valid]
        v = velocity[keep]
        if random_walk:
            renew = eddy_left[keep] <= 0.0
            if renew.any():
                sigma = np.sqrt(2.0 * k[renew] / 3.0)
                fluct[keep[renew]] = rng.standard_normal((int(renew.sum()), 3)) * sigma[:, None]
                lifetime = 0.3 / np.maximum(omega[renew], 1e-3)
                eddy_left[keep[renew]] = -lifetime * np.log(np.maximum(rng.random(int(renew.sum())), 1e-12))
            u = u + fluct[keep]
        slip = np.linalg.norm(u - v, axis=1)
        reynolds = air * slip * d / mu
        tau = tau0 / (1.0 + 0.15 * reynolds**0.687)
        speed = np.maximum(np.linalg.norm(v, axis=1), 0.05)
        dt = np.minimum(0.3 * near / speed, 0.01)
        target = u + tau[:, None] * gravity
        decay = np.exp(-dt / tau)[:, None]
        new_v = target + (v - target) * decay
        # Exact position update for the exponential velocity relaxation.
        displacement = target * dt[:, None] + (v - target) * tau[:, None] * (1.0 - decay)
        positions[keep] += displacement
        velocity[keep] = new_v
        eddy_left[keep] -= dt
        elapsed[keep] += dt
        entered[keep] |= inside_housing(positions[keep])
        # Past the housing and outside it: gone downstream.
        gone = keep[(positions[keep, 0] > hi[0] + 0.5 * ref) | (elapsed[keep] > max_time_s)]
        state[gone] = np.where(elapsed[gone] > max_time_s, 4, 3)
    return DropletResult(
        injected=count,
        entered=int(entered.sum()),
        sensor_hits=int(np.sum(state == 1)),
        trapped_housing=int(np.sum(state == 2)),
        escaped=int(np.sum(state == 3)),
        unresolved=int(np.sum((state == 0) | (state == 4))),
    )


# ---------------------------------------------------------------------------
# Studies
# ---------------------------------------------------------------------------


def prepare_study(
    setup: Sps30Setup,
    cfd: Sps30CfdSettings,
    points: Sequence[Sps30Point],
    study_dir: Path | str,
    on_line: Callable[[str], None] | None = None,
) -> Path:
    from backend.sps30_study import write_points_csv

    study_dir = Path(study_dir)
    study_dir.mkdir(parents=True, exist_ok=True)
    say = on_line or (lambda _l: None)
    say(f"Meshing the air round the housing ({cfd.mesh_resolution})...")
    domain = mesh_sps30_domain(setup, study_dir, cfd.mesh_resolution)
    say(f"  {domain.cell_count:,} cells; sensor face {domain.marker_counts.get('SENSOR', 0)} facets")
    (study_dir / "study.json").write_text(
        json.dumps({"setup": setup.model_dump(mode="json"), "cfd": cfd.model_dump(mode="json")}, indent=2)
    )
    write_points_csv(study_dir / "design_points.csv", points)
    root = Path(__file__).resolve().parent.parent
    python = sys.executable or "python"
    (study_dir / "run_design_points.bat").write_text(
        f'@echo off\r\ncd /d "{root}"\r\n"{python}" -m backend.sps30_cfd "%~dp0."\r\npause\r\n'
    )
    (study_dir / "run_design_points.sh").write_text(
        f'#!/bin/sh\ncd "{root}"\n"{python}" -m backend.sps30_cfd "$(dirname "$0")"\n'
    )
    return study_dir


def load_study(study_dir: Path) -> tuple[Sps30Setup, Sps30CfdSettings, Sps30Domain]:
    manifest = json.loads((study_dir / "study.json").read_text())
    return (
        Sps30Setup.model_validate(manifest["setup"]),
        Sps30CfdSettings.model_validate(manifest["cfd"]),
        Sps30Domain.load(study_dir),
    )


def run_design_point(
    study_dir: Path | str, point: Sps30Point, runner, on_line: Callable[[str], None] | None = None
) -> Sps30Point:
    from backend.su2_config import write_config
    from backend.su2_parser import SU2OutputParser
    from backend.visualizer import find_solution_file, load_solution

    study_dir = Path(study_dir)
    setup, cfd, domain = load_study(study_dir)
    case = study_dir / "points" / point.name
    case.mkdir(parents=True, exist_ok=True)
    mesh = case / MESH_FILENAME
    if not mesh.exists():
        shutil.copyfile(study_dir / MESH_FILENAME, mesh)
    config = write_config(
        build_sps30_config(setup, cfd, point, domain_normal=domain.sensor_normal), case / "air.cfg"
    )
    parser = SU2OutputParser()
    say = on_line or (lambda _l: None)
    say(f"{point.name}: air flow (SU2)")

    def handle(line: str) -> None:
        parser.feed(line)
        if on_line is not None:
            on_line(line)

    started = time.perf_counter()
    outcome = runner.run(config_path=config, working_directory=case, ranks=cfd.mpi_ranks, on_line=handle)
    if parser.errors:
        raise Sps30CfdError(f"{point.name}: SU2 reported an error:\n" + "\n".join(parser.errors[:5]))
    if not outcome.succeeded:
        raise Sps30CfdError(f"{point.name}: SU2 stopped with code {outcome.return_code}")
    dataset = load_solution(find_solution_file(case))
    face = face_velocity(dataset, domain)
    exchange = exchange_flow_lpm(dataset, domain)
    say(f"{point.name}: tracking {cfd.droplets} droplets of {point.droplet_um:g} um")
    walls = dict(np.load(study_dir / "walls.npz"))
    drops = track_droplets(
        dataset, domain, setup, point, cfd.droplets, cfd.random_walk, cfd.seed, walls=walls
    )
    solved = point.model_copy(
        update={
            "face_velocity_ms": face,
            "penetration": drops.penetration,
            "exchange_flow_lpm": exchange,
            "sensor_hits": drops.sensor_hits,
            "droplets": drops.entered,
            "source": "su2",
        }
    )
    (case / "result.json").write_text(
        json.dumps(
            {
                **solved.model_dump(mode="json"),
                "droplets_injected": drops.injected,
                "droplets_entered": drops.entered,
                "trapped_housing": drops.trapped_housing,
                "escaped": drops.escaped,
                "unresolved": drops.unresolved,
                "wall_time_s": time.perf_counter() - started,
            },
            indent=2,
        )
    )
    say(
        f"{point.name}: face {face:.3f} m/s, exchange {exchange:.2f} L/min, "
        f"{drops.sensor_hits} of {drops.entered} droplets inside reached the sensor"
    )
    return solved


def solved_points(study_dir: Path | str) -> list[Sps30Point]:
    from backend.sps30_study import read_points_csv

    study_dir = Path(study_dir)
    setup = load_study(study_dir)[0]
    out = []
    for point in read_points_csv(study_dir / "design_points.csv", setup, source=""):
        result = study_dir / "points" / point.name / "result.json"
        if not point.solved() and result.is_file():
            data = json.loads(result.read_text())
            point = Sps30Point.model_validate({k: data[k] for k in Sps30Point.model_fields})
        out.append(point)
    return out


def main(argv: Sequence[str] | None = None) -> int:
    from backend.runner import SU2Runner

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print("usage: python -m backend.sps30_cfd <study_dir>")
        return 2
    folder = Path(arguments[0])
    for point in solved_points(folder):
        if not point.solved():
            run_design_point(folder, point, SU2Runner(), on_line=print)
    done = [p for p in solved_points(folder) if p.solved()]
    print(f"{len(done)} design points solved")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
