"""A quick picture of the setup, for the operator to approve before a solve.

The assistant chooses which way the air comes from and which axis the model
tilts about, and a wrong guess costs a whole mesh and solve before anyone
notices -- a rocket flown tail first returns perfectly plausible numbers.
So before it meshes or solves with a setup the operator has not seen, it
draws this: the model coarsely tessellated, in rocket axes as the program
shows it, with the oncoming air and the tilt axis drawn over it exactly as
the Aerodynamics tab draws them. Two views side by side -- from the side,
where the angle of attack reads directly, and from an angle, where the tilt
axis does.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")

# Deliberately small: this is a check, and it should be on screen in seconds.
PREVIEW_TRIANGLES = 6000
PREVIEW_SIZE = (1200, 560)
MODEL_COLOUR = "#8aa0c0"


def render_orientation_preview(
    geometry,
    output_path: Path | str,
    aoa_deg: float = 0.0,
    sideslip_deg: float = 0.0,
    caption: str = "",
    pivot_height_m: float = 0.0,
) -> Path:
    """Draw the model with the air and the tilt axis, and save it as PNG."""
    import pyvista as pv

    from backend.mesh_pipeline import tessellate_geometry
    from backend.visualizer import ROCKET_CAMERA_VIEWS
    from core.frames import points_to_rocket
    from gui.flow_overlay import build_overlay

    pv.OFF_SCREEN = True
    preview = tessellate_geometry(geometry, target_triangles=PREVIEW_TRIANGLES)
    faces = np.hstack(
        [np.full((len(preview.triangles), 1), 3, dtype=np.int64), preview.triangles]
    ).ravel()
    surface = pv.PolyData(points_to_rocket(preview.points), faces)
    overlay = build_overlay(
        tuple(surface.bounds), aoa_deg, sideslip_deg, pivot_height_m
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plotter = pv.Plotter(off_screen=True, shape=(1, 2), window_size=PREVIEW_SIZE)
    try:
        plotter.set_background("#1b2130", top="#0e1118")
        for column, (view, label) in enumerate(
            (("side", "Side view"), ("isometric", "Angled view"))
        ):
            plotter.subplot(0, column)
            plotter.add_mesh(surface, color=MODEL_COLOUR, smooth_shading=True)
            for mesh, options in overlay:
                plotter.add_mesh(mesh, **options)
            direction, up = ROCKET_CAMERA_VIEWS.get(view, ROCKET_CAMERA_VIEWS["isometric"])
            plotter.view_vector(direction, up)
            plotter.reset_camera()
            plotter.add_axes(color="white")
            plotter.add_text(label, position="upper_left", font_size=11, color="white")
            if column == 0 and caption:
                plotter.add_text(caption, position="lower_left", font_size=9, color="white")
        plotter.screenshot(str(output_path))
    finally:
        plotter.close()
    return output_path
