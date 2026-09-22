"""The two coordinate frames a rocket lives in, and the one rotation between them.

**Solver frame** (the wind tunnel). Meshing rotates the CAD so the body runs
from the nose at the origin towards +X, and the flow arrives along +X. SU2's
angle-of-attack and sideslip conventions assume exactly this, so the mesh,
the solver and its raw force output all stay in it.

**Rocket frame** (what the operator sees). The same body stood on its tail:
the nose points to **+Z**, as it does on the pad and in vertical flight. The
origin is the same point -- the reference origin, normally the nose tip -- so
the body occupies negative Z. Every coordinate shown to the operator -- the
imported model, rendered images, force components, the centre of pressure
and fin hinge axes -- is in this frame, whichever way the CAD was drawn.

The rotation between them is fixed:

    x_rocket =  z_solver
    y_rocket =  y_solver
    z_rocket = -x_solver

It is a proper rotation (determinant +1), so handedness, cross products and
moments carry over unchanged. Two consequences worth knowing:

* Angle of attack pitches the nose in the rocket **X-Z** plane, and the lift
  it produces points along rocket **+X**. Sideslip yaws in the **Y-Z** plane.
* Drag pushes the rocket towards **-Z**, against its direction of flight, so
  a rocket climbing straight up reports its drag as a negative Z force.

The rocket frame is a view, not a second geometry: nothing is re-meshed, and a
mesh made before this module existed is shown correctly too.
"""

from __future__ import annotations

from enum import Enum
from typing import Sequence

import numpy as np

# Rows are the rocket axes expressed in solver coordinates.
SOLVER_TO_ROCKET = np.array(
    [
        [0.0, 0.0, 1.0],
        [0.0, 1.0, 0.0],
        [-1.0, 0.0, 0.0],
    ]
)
ROCKET_TO_SOLVER = SOLVER_TO_ROCKET.T

# One sentence for anything that reports rocket-frame numbers to a reader.
AXIS_CONVENTION = (
    "Rocket frame: +Z points along the nose (straight up for a rocket in "
    "vertical flight), origin at the reference origin (the nose tip by "
    "default). Drag on a rocket flying nose-first is a negative F_z; the lift "
    "from a positive angle of attack is +F_x; sideslip produces F_y."
)


class Frame(str, Enum):
    """Which frame a coordinate is expressed in."""

    ROCKET = "rocket"
    SOLVER = "solver"


def to_rocket(vector: Sequence[float]) -> list[float]:
    """Express a solver-frame point or vector in the rocket frame."""
    return [float(v) for v in SOLVER_TO_ROCKET @ np.asarray(vector, dtype=float)]


def to_solver(vector: Sequence[float]) -> list[float]:
    """Express a rocket-frame point or vector in the solver frame."""
    return [float(v) for v in ROCKET_TO_SOLVER @ np.asarray(vector, dtype=float)]


def points_to_rocket(points: np.ndarray) -> np.ndarray:
    """Rotate an ``(n, 3)`` array of solver-frame points into the rocket frame."""
    return np.asarray(points, dtype=float) @ SOLVER_TO_ROCKET.T


def homogeneous_solver_to_rocket() -> np.ndarray:
    """The rotation as a 4x4 matrix, for mesh libraries that want one."""
    matrix = np.eye(4)
    matrix[:3, :3] = SOLVER_TO_ROCKET
    return matrix
