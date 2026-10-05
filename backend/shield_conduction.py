"""Heat conduction inside the shield: the solid half of the conjugate solve.

SU2 solves the air; this solves the plates. They meet at the shield's
surface and are coupled by the program, pass by pass (a partitioned
conjugate-heat-transfer scheme):

1. Here, the steady heat equation ``div(k grad T) = 0`` in the solid, on a
   tetrahedral mesh of the shield bodies (linear finite elements). Every
   surface triangle carries a Robin condition holding what arrives and
   leaves there:

   ``q_in(T) = absorbed - e*sigma*F*T^4 - [q_f + h_f (T - T_f)]``

   -- the sun and long-wave it absorbs, its own emission to the sky and
   the ground it sees, and the heat the air takes away, linearised about
   the air side's last answer (SU2's wall heat flux ``q_f`` at wall
   temperature ``T_f``, with a film coefficient ``h_f``). The T^4 term is
   Newton-linearised and refreshed three times.
2. SU2 then solves the air with the shield walls held at the solid's
   surface temperature (grouped into a few isothermal markers), and
   reports the heat it takes from each facet. Repeat until the surface
   temperature stops changing.

This is what carries the sun absorbed on the top of an aluminium plate
through the metal to the faces round the thermometer -- which a model of
independent wall facets cannot do.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from core.shield_models import STEFAN_BOLTZMANN

SOLID_FILENAME = "solid_mesh.npz"
# Film coefficient bounds when estimating the air side, W/(m^2 K).
H_MIN, H_MAX = 1.0, 250.0


class ConductionError(RuntimeError):
    """The solid could not be meshed or solved."""


# ---------------------------------------------------------------------------
# Solid mesh
# ---------------------------------------------------------------------------


def mesh_solids(step_path: Path, scale: float, centre: np.ndarray, size: float) -> dict:
    """Tetrahedral mesh of every shield body, placed where the domain has it.

    Returns ``points`` (N, 3), ``tets`` (M, 4) and ``body`` (M,) -- which
    solid each tetrahedron belongs to.
    """
    import gmsh

    from backend.gmsh_session import gmsh_session

    with gmsh_session("shield_solids"):
        occ = gmsh.model.occ
        solids = [e for e in occ.importShapes(str(step_path)) if e[0] == 3]
        if not solids:
            raise ConductionError(f"{Path(step_path).name} contains no solid")
        if scale != 1.0:
            occ.dilate(solids, 0.0, 0.0, 0.0, scale, scale, scale)
        occ.synchronize()
        low = np.full(3, np.inf)
        high = np.full(3, -np.inf)
        for dim, tag in solids:
            box = gmsh.model.getBoundingBox(dim, tag)
            low, high = np.minimum(low, box[:3]), np.maximum(high, box[3:])
        occ.translate(solids, *(np.asarray(centre) - 0.5 * (low + high)))
        # Separate solids that touch must share their faces to conduct.
        occ.removeAllDuplicates()
        occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", size)
        gmsh.option.setNumber("Mesh.MeshSizeMin", 0.25 * size)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 12)
        gmsh.option.setNumber("Mesh.Algorithm3D", 1)
        gmsh.model.mesh.generate(3)
        tags, coords, _ = gmsh.model.mesh.getNodes()
        index = np.full(int(tags.max()) + 1, -1, dtype=np.int64)
        index[tags.astype(np.int64)] = np.arange(len(tags))
        points = coords.reshape(-1, 3)
        tets, bodies = [], []
        for number, (_, tag) in enumerate(gmsh.model.getEntities(3)):
            types, _, nodes = gmsh.model.mesh.getElements(3, tag)
            for kind, block in zip(types, nodes):
                if kind == 4:
                    cells = index[block.astype(np.int64)].reshape(-1, 4)
                    tets.append(cells)
                    bodies.append(np.full(len(cells), number))
    if not tets:
        raise ConductionError("the shield solids produced no tetrahedra")
    tets = np.vstack(tets)
    used = np.unique(tets)
    remap = np.full(len(points), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    return {"points": points[used], "tets": remap[tets], "body": np.concatenate(bodies)}


def save_solid(folder: Path, mesh: dict) -> Path:
    path = Path(folder) / SOLID_FILENAME
    np.savez_compressed(path, **mesh)
    return path


def load_solid(folder: Path) -> dict | None:
    path = Path(folder) / SOLID_FILENAME
    if not path.is_file():
        return None
    data = np.load(path)
    return {key: data[key] for key in data.files}


# ---------------------------------------------------------------------------
# Finite elements
# ---------------------------------------------------------------------------


def boundary_triangles(tets: np.ndarray) -> np.ndarray:
    """Faces of the tetrahedra that belong to only one of them."""
    faces = np.vstack([tets[:, [0, 1, 2]], tets[:, [0, 1, 3]], tets[:, [0, 2, 3]], tets[:, [1, 2, 3]]])
    key = np.sort(faces, axis=1)
    _, first, counts = np.unique(key, axis=0, return_index=True, return_counts=True)
    return faces[first[counts == 1]]


@dataclass
class SolidSurfaceState:
    """What the air and the radiation do at each solid surface triangle."""

    absorbed: np.ndarray    # W/m^2 absorbed (sun + long-wave)
    emissivity: np.ndarray  # -
    view: np.ndarray        # view factor to sky + ground
    q_air: np.ndarray       # W/m^2 the air took at the last air solve
    t_air_wall: np.ndarray  # K, wall temperature at that solve
    h_air: np.ndarray       # W/(m^2 K), film coefficient


class SolidConduction:
    """Steady conduction in the shield with Robin conditions on its surface."""

    def __init__(self, points: np.ndarray, tets: np.ndarray, conductivity: float) -> None:
        from scipy.sparse import coo_matrix

        self.points = np.asarray(points, dtype=float)
        self.tets = np.asarray(tets, dtype=np.int64)
        self.conductivity = float(conductivity)
        corners = self.points[self.tets]
        jac = np.stack(
            [corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0], corners[:, 3] - corners[:, 0]],
            axis=2,
        )
        det = np.linalg.det(jac)
        keep = np.abs(det) > 1e-30
        jac, det, tets = jac[keep], det[keep], self.tets[keep]
        inverse = np.linalg.inv(jac)
        reference = np.array([[-1.0, -1.0, -1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        gradients = np.einsum("aj,ejk->eak", reference, inverse)
        volume = np.abs(det) / 6.0
        local = self.conductivity * volume[:, None, None] * np.einsum(
            "eak,ebk->eab", gradients, gradients
        )
        rows = np.repeat(tets, 4, axis=1).ravel()
        cols = np.tile(tets, (1, 4)).ravel()
        size = len(self.points)
        self.stiffness = coo_matrix((local.ravel(), (rows, cols)), shape=(size, size)).tocsr()
        self.surface = boundary_triangles(tets)
        triangle = self.points[self.surface]
        cross = np.cross(triangle[:, 1] - triangle[:, 0], triangle[:, 2] - triangle[:, 0])
        self.areas = 0.5 * np.linalg.norm(cross, axis=1)
        self.centroids = triangle.mean(axis=1)

    def solve(self, state: SolidSurfaceState, guess: float, newton: int = 3) -> np.ndarray:
        """Node temperatures for this surface state, K."""
        from scipy.sparse import coo_matrix
        from scipy.sparse.linalg import spsolve

        size = len(self.points)
        temperature = np.full(size, float(guess))
        mass = np.array([[2.0, 1.0, 1.0], [1.0, 2.0, 1.0], [1.0, 1.0, 2.0]]) / 12.0
        rows = np.repeat(self.surface, 3, axis=1).ravel()
        cols = np.tile(self.surface, (1, 3)).ravel()
        for _ in range(newton):
            wall = temperature[self.surface].mean(axis=1)
            radiative = STEFAN_BOLTZMANN * state.emissivity * state.view
            # q_in = Q - H T, from the linearised emission and air film.
            h_total = 4.0 * radiative * wall**3 + state.h_air
            source = (
                state.absorbed
                + 3.0 * radiative * wall**4
                - state.q_air
                + state.h_air * state.t_air_wall
            )
            local = (h_total * self.areas)[:, None, None] * mass
            robin = coo_matrix((local.ravel(), (rows, cols)), shape=(size, size)).tocsr()
            load = np.zeros(size)
            np.add.at(load, self.surface.ravel(), np.repeat(source * self.areas / 3.0, 3))
            temperature = spsolve(self.stiffness + robin, load)
        return temperature

    def surface_temperature(self, temperature: np.ndarray) -> np.ndarray:
        """Mean temperature of every surface triangle."""
        return temperature[self.surface].mean(axis=1)


def match_surfaces(source_centroids: np.ndarray, target_centroids: np.ndarray) -> np.ndarray:
    """For every target triangle, the nearest source triangle."""
    from scipy.spatial import cKDTree

    _, index = cKDTree(source_centroids).query(target_centroids)
    return index


def film_coefficient(q_air: np.ndarray, t_wall: np.ndarray, ambient: float,
                     previous: np.ndarray) -> np.ndarray:
    """Air-side film coefficient from the last air solve, kept where undefined."""
    difference = t_wall - ambient
    estimate = np.where(np.abs(difference) > 0.02, q_air / np.where(difference == 0, 1, difference),
                        previous)
    return np.clip(np.where(np.isfinite(estimate) & (estimate > 0), estimate, previous), H_MIN, H_MAX)
