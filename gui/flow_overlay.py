"""Arrows showing where the air comes from and what the model tilts about.

Drawn over the imported model in the viewport, in rocket axes, so the setup
can be checked by eye before a mesh is paid for: the oncoming air as a grid
of arrows arriving at the angle of attack and sideslip currently set, and
the tilt axis as a line through the model with a curved arrow round it.

The directions follow the solver's own conventions exactly. SU2 sets the
freestream to (cos a cos b, sin b, sin a cos b) in its frame; turned into
rocket axes (see core.frames) that is the vector used here, and the tilt
axis is the solver's Y -- the axis the angle of attack rotates the flow
about, which is where the chosen pitch axis is placed when meshing.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

FLOW_COLOUR = "#4da3ff"
TILT_COLOUR = "#f0a030"

# Arrow grid across the flow, and its spacing in body widths.
ARROW_ROWS = 3


def freestream_direction(aoa_deg: float, sideslip_deg: float) -> np.ndarray:
    """Unit vector the air moves along, in rocket axes."""
    alpha = math.radians(aoa_deg)
    beta = math.radians(sideslip_deg)
    solver = np.array(
        [math.cos(alpha) * math.cos(beta), math.sin(beta), math.sin(alpha) * math.cos(beta)]
    )
    from core.frames import SOLVER_TO_ROCKET

    vector = SOLVER_TO_ROCKET @ solver
    return vector / np.linalg.norm(vector)


def tilt_axis_direction() -> np.ndarray:
    """The axis an angle of attack tilts the model about, in rocket axes."""
    return np.array([0.0, 1.0, 0.0])


def build_overlay(
    bounds: tuple[float, float, float, float, float, float],
    aoa_deg: float = 0.0,
    sideslip_deg: float = 0.0,
) -> list[tuple[Any, dict[str, Any]]]:
    """Meshes and drawing options for the flow arrows and the tilt axis.

    ``bounds`` are the model's, in rocket axes. Returns ``(mesh, kwargs)``
    pairs ready for a PyVista plotter's ``add_mesh``.
    """
    import pyvista as pv

    low = np.array(bounds[::2], dtype=float)
    high = np.array(bounds[1::2], dtype=float)
    centre = 0.5 * (low + high)
    extent = high - low
    length = float(max(extent))
    width = float(max(extent[0], extent[1], 1.0e-3 * length))

    flow = freestream_direction(aoa_deg, sideslip_deg)
    # Arrows start upstream of the model and point along the flow.
    arrow_length = 0.28 * length
    upstream = centre - flow * (0.5 * float(np.abs(extent @ np.abs(flow))) + 0.45 * length)
    across_a = np.cross(flow, [0.0, 1.0, 0.0])
    if np.linalg.norm(across_a) < 1.0e-6:
        across_a = np.cross(flow, [1.0, 0.0, 0.0])
    across_a /= np.linalg.norm(across_a)
    across_b = np.cross(flow, across_a)
    spacing = max(width, 0.12 * length)

    items: list[tuple[Any, dict[str, Any]]] = []
    offsets = np.linspace(-1.0, 1.0, ARROW_ROWS) * spacing
    for a in offsets:
        for b in offsets:
            start = upstream + a * across_a + b * across_b
            arrow = pv.Arrow(
                start=start,
                direction=flow,
                scale=arrow_length,
                tip_length=0.3,
                tip_radius=0.08,
                shaft_radius=0.025,
            )
            items.append((arrow, {"color": FLOW_COLOUR, "opacity": 0.85}))

    # The tilt axis: a rod through the model, sticking out either side.
    axis = tilt_axis_direction()
    reach = 0.5 * float(np.abs(extent @ axis)) + 0.35 * length
    rod = pv.Cylinder(
        center=centre,
        direction=axis,
        radius=0.008 * length,
        height=2.0 * reach,
    )
    items.append((rod, {"color": TILT_COLOUR}))

    # A curved arrow round the axis, showing the sense of a positive angle.
    radius = 0.12 * length
    start_angle, sweep = math.radians(-40.0), math.radians(260.0)
    ring_centre = centre + axis * reach * 0.85
    u = np.array([1.0, 0.0, 0.0])
    w = np.cross(axis, u)
    angles = np.linspace(start_angle, start_angle + sweep, 48)
    arc_points = np.array(
        [ring_centre + radius * (math.cos(t) * u + math.sin(t) * w) for t in angles]
    )
    arc = pv.Spline(arc_points, 96).tube(radius=0.006 * length)
    items.append((arc, {"color": TILT_COLOUR}))
    tip_direction = arc_points[-1] - arc_points[-2]
    tip = pv.Cone(
        center=arc_points[-1] + tip_direction / np.linalg.norm(tip_direction) * 0.02 * length,
        direction=tip_direction,
        height=0.05 * length,
        radius=0.018 * length,
    )
    items.append((tip, {"color": TILT_COLOUR}))
    return items


def describe(aoa_deg: float, sideslip_deg: float, pitch_axis: str | None) -> str:
    """One line saying what the arrows show."""
    axis = f"CAD {pitch_axis}" if pitch_axis else "the automatic axis"
    return (
        f"Blue: oncoming air at alpha {aoa_deg:.1f} deg, beta "
        f"{sideslip_deg:.1f} deg. Orange: tilt axis ({axis}), shown as rocket Y."
    )
