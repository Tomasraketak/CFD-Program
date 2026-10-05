"""Pictures of the solved air round a shield or inside a housing.

A sensor study solves a 2 m tunnel round a 5 cm shield, so a picture of the
whole domain shows nothing; these renders crop to the object, cut a plane
through the thermometer (or the SPS30 intake), and draw the object itself
over the field:

* ``temperature`` -- air temperature on the plane, with the inlet value and
  the thermometer reading in the caption;
* ``velocity`` -- air speed on the plane with small arrows;
* ``streamlines`` -- the paths of the air through and round the object;
* ``wall_temperature`` -- the object's surface coloured by temperature.

Each point of a study keeps its full solution (``points/<name>/flow.vtu``),
so any solved point can be drawn without solving it again.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")

QUANTITIES = ("temperature", "velocity", "streamlines", "wall_temperature")
PLANES = {"y": (0.0, 1.0, 0.0), "x": (1.0, 0.0, 0.0), "z": (0.0, 0.0, 1.0)}
SIZE = (1600, 1000)


class StudyRenderError(RuntimeError):
    """The point cannot be drawn."""


def _study_geometry(study_dir: Path, kind: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    """Object bounding box (low, high), the point of interest and its name."""
    geometry = json.loads((study_dir / "geometry.json").read_text())
    if kind == "shield":
        manifest = json.loads((study_dir / "study.json").read_text())
        centre = np.array(geometry["shield_centre_m"], dtype=float)
        size = np.array(geometry["shield_size_m"], dtype=float)
        offset = np.array(manifest["setup"].get("thermometer_xyz_m", [0, 0, 0]), dtype=float)
        return centre - 0.5 * size, centre + 0.5 * size, centre + offset, "thermometer"
    low = np.array(geometry["housing_low"], dtype=float)
    high = np.array(geometry["housing_high"], dtype=float)
    return low, high, np.array(geometry["sensor_center"], dtype=float), "SPS30 intake"


def _object_surface(grid, low: np.ndarray, high: np.ndarray):
    """The walls of the object: boundary faces inside its (padded) box."""
    surface = grid.extract_surface()
    pad = 0.02 * float(np.max(high - low)) + 1e-6
    centres = surface.cell_centers().points
    inside = np.all((centres >= low - pad) & (centres <= high + pad), axis=1)
    return surface.extract_cells(np.flatnonzero(inside)).extract_surface()


def _ambient(study_dir: Path, kind: str) -> float | None:
    try:
        setup = json.loads((study_dir / "study.json").read_text())["setup"]
        return float(setup["ambient_temp_c"]) + 273.15
    except (OSError, KeyError, ValueError):
        return None


def render_study_point(
    study_dir: Path | str,
    kind: str,
    point: str,
    quantity: str,
    output_path: Path | str,
    plane: str = "y",
) -> Path:
    """Draw one quantity of one solved point and save it as PNG."""
    import pyvista as pv

    from backend.visualizer import find_solution_file, resolve_field

    if quantity not in QUANTITIES:
        raise StudyRenderError(f"quantity must be one of {', '.join(QUANTITIES)}")
    if plane not in PLANES:
        raise StudyRenderError("plane must be 'x', 'y' or 'z'")
    study_dir = Path(study_dir)
    case = study_dir / "points" / point
    if not case.is_dir():
        raise StudyRenderError(f"point {point} has not been solved in this study")
    try:
        grid = pv.read(str(find_solution_file(case)))
    except Exception as error:  # noqa: BLE001 - reported with context
        raise StudyRenderError(f"no solution for {point}: {error}") from error
    pv.OFF_SCREEN = True

    low, high, probe, probe_name = _study_geometry(study_dir, kind)
    span = float(np.max(high - low))
    crop_low = low - np.array([1.2, 1.0, 1.0]) * span
    crop_high = high + np.array([2.0, 1.0, 1.0]) * span
    region = grid.clip_box(
        bounds=(crop_low[0], crop_high[0], crop_low[1], crop_high[1], crop_low[2], crop_high[2]),
        invert=False,
    )
    temperature = resolve_field(grid, "temperature") if "Temperature" in grid.point_data else None
    velocity = resolve_field(grid, "velocity")
    region.point_data["speed"] = np.linalg.norm(np.asarray(region.point_data[velocity]), axis=1)
    walls = _object_surface(grid, low, high)
    ambient = _ambient(study_dir, kind)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plotter = pv.Plotter(off_screen=True, window_size=SIZE)
    plotter.set_background("#1b2130", top="#0e1118")
    bar = {"color": "white", "title_font_size": 16, "label_font_size": 14, "fmt": "%.2f",
           "vertical": True, "position_x": 0.86, "position_y": 0.15, "height": 0.7}
    normal = PLANES[plane]
    caption = f"{point}"

    if quantity in ("temperature", "velocity"):
        cut = region.slice(normal=normal, origin=probe)
        if cut.n_points == 0:
            raise StudyRenderError("the cutting plane misses the solution")
        if quantity == "temperature":
            if temperature is None:
                raise StudyRenderError("this solution has no temperature")
            plotter.add_mesh(cut, scalars=temperature, cmap="coolwarm",
                             scalar_bar_args={**bar, "title": "Air temperature [K]"})
        else:
            plotter.add_mesh(cut, scalars="speed", cmap="turbo",
                             scalar_bar_args={**bar, "title": "Air speed [m/s]"})
            # A coarse, even grid of arrows on the plane: the direction of the
            # air, without hiding the colours.
            axes = [i for i in range(3) if normal[i] == 0.0]
            u = np.linspace(crop_low[axes[0]], crop_high[axes[0]], 28)
            v = np.linspace(crop_low[axes[1]], crop_high[axes[1]], 18)
            grid_points = np.zeros((u.size * v.size, 3))
            grid_points[:, axes[0]] = np.repeat(u, v.size)
            grid_points[:, axes[1]] = np.tile(v, u.size)
            grid_points[:, [i for i in range(3) if i not in axes][0]] = probe[
                [i for i in range(3) if i not in axes][0]
            ]
            arrows = pv.PolyData(grid_points).sample(grid)
            vectors = np.asarray(arrows.point_data[velocity], dtype=float)
            speed = np.linalg.norm(vectors, axis=1)
            keep = speed > 1e-6
            if keep.any():
                arrows = arrows.extract_points(np.flatnonzero(keep))
                arrows.point_data["v"] = vectors[keep] / max(float(speed.max()), 1e-9)
                plotter.add_mesh(
                    arrows.glyph(orient="v", scale="v", factor=0.12 * span),
                    color="white", opacity=0.75,
                )
        plotter.add_mesh(walls.clip(normal=normal, origin=probe, invert=False),
                         color="#c9d3e2", opacity=1.0)
        view = {"y": ((0, -1, 0), (0, 0, 1)), "x": ((-1, 0, 0), (0, 0, 1)),
                "z": ((0, 0, 1), (0, 1, 0))}[plane]
    elif quantity == "streamlines":
        seeds = pv.Line(
            (crop_low[0] + 0.1 * span, probe[1], low[2] - 0.3 * span),
            (crop_low[0] + 0.1 * span, probe[1], high[2] + 0.3 * span),
            resolution=40,
        )
        lines = region.streamlines_from_source(
            seeds, vectors=velocity, max_length=10.0 * span,
            integration_direction="forward",
        )
        if lines.n_points:
            plotter.add_mesh(lines.tube(radius=0.004 * span), scalars="speed"
                             if "speed" in lines.point_data else None, cmap="turbo",
                             scalar_bar_args={**bar, "title": "Air speed [m/s]"})
        plotter.add_mesh(walls, color="#c9d3e2", opacity=0.55)
        view = ((0.3, -1.0, 0.35), (0, 0, 1))
    else:  # wall_temperature
        if temperature is None or temperature not in walls.point_data:
            raise StudyRenderError("this solution has no wall temperature")
        plotter.add_mesh(walls, scalars=temperature, cmap="inferno",
                         scalar_bar_args={**bar, "title": "Wall temperature [K]"})
        view = ((0.6, -1.0, 0.7), (0, 0, 1))

    plotter.add_mesh(pv.Sphere(radius=0.03 * span, center=probe), color="#ef5350")
    if temperature is not None and ambient is not None:
        try:
            probed = pv.PolyData(np.array([probe])).sample(grid)
            reading = float(np.asarray(probed.point_data[temperature])[0])
            caption += (f" | inlet {ambient:.2f} K | {probe_name} {reading:.2f} K "
                        f"(dT {reading - ambient:+.3f} K)")
        except Exception:  # noqa: BLE001 - caption only
            pass
    titles = {"temperature": "Air temperature", "velocity": "Air speed",
              "streamlines": "Streamlines", "wall_temperature": "Wall temperature"}
    plotter.add_text(f"{titles[quantity]} -- {caption}", position="upper_left",
                     font_size=11, color="white")
    plotter.view_vector(*view)
    plotter.reset_camera()
    plotter.camera.zoom(1.15)
    try:
        plotter.screenshot(str(output_path))
    finally:
        plotter.close()
    return output_path
