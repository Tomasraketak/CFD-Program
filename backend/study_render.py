"""Pictures of the solved air round a shield or inside a housing.

A sensor study solves a 2 m tunnel round a 5 cm shield, so a picture of the
whole domain shows nothing; these renders crop to the object, cut a plane
through the thermometer (or the SPS30 intake), and draw the object itself
over the field:

* ``temperature`` -- air temperature on the plane, with the inlet value and
  the thermometer reading in the caption;
* ``velocity`` -- air speed on the plane with small arrows;
* ``streamlines`` -- the paths of the air through and round the object;
* ``wall_temperature`` -- the object's surface coloured by temperature;
* ``overview`` -- all four in one 2x2 picture.

Each point of a study keeps its full solution (``points/<name>/flow.vtu``),
so any solved point can be drawn without solving it again.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")

QUANTITIES = ("overview", "temperature", "velocity", "streamlines", "wall_temperature")
PLANES = {"y": (0.0, 1.0, 0.0), "x": (1.0, 0.0, 0.0), "z": (0.0, 0.0, 1.0)}
SINGLE_QUANTITIES = QUANTITIES[1:]
SIZE = (1920, 1200)
OVERVIEW_SIZE = (2400, 1600)


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


class _Context:
    """Everything a panel needs, loaded once per point."""

    def __init__(self, study_dir: Path, kind: str, point: str, plane: str) -> None:
        import pyvista as pv

        from backend.visualizer import find_solution_file, resolve_field

        case = study_dir / "points" / point
        if not case.is_dir():
            raise StudyRenderError(f"point {point} has not been solved in this study")
        try:
            self.grid = pv.read(str(find_solution_file(case)))
        except Exception as error:  # noqa: BLE001 - reported with context
            raise StudyRenderError(f"no solution for {point}: {error}") from error
        self.point, self.plane, self.kind = point, plane, kind
        self.low, self.high, self.probe, self.probe_name = _study_geometry(study_dir, kind)
        self.span = float(np.max(self.high - self.low))
        span = self.span
        self.crop_low = self.low - np.array([1.2, 1.0, 1.0]) * span
        self.crop_high = self.high + np.array([2.0, 1.0, 1.0]) * span
        b = (self.crop_low[0], self.crop_high[0], self.crop_low[1], self.crop_high[1],
             self.crop_low[2], self.crop_high[2])
        self.region = self.grid.clip_box(bounds=b, invert=False)
        self.temperature = (resolve_field(self.grid, "temperature")
                            if "Temperature" in self.grid.point_data else None)
        self.velocity = resolve_field(self.grid, "velocity")
        self.region.point_data["speed"] = np.linalg.norm(
            np.asarray(self.region.point_data[self.velocity]), axis=1)
        self.walls = _object_surface(self.grid, self.low, self.high)
        self.ambient = _ambient(study_dir, kind)
        self.reading = None
        if self.temperature is not None:
            try:
                probed = pv.PolyData(np.array([self.probe])).sample(self.grid)
                self.reading = float(np.asarray(probed.point_data[self.temperature])[0])
            except Exception:  # noqa: BLE001 - caption only
                self.reading = None

    def caption(self) -> str:
        text = self.point
        if self.reading is not None and self.ambient is not None:
            text += (f" | inlet {self.ambient:.2f} K | {self.probe_name} {self.reading:.2f} K "
                     f"(dT {self.reading - self.ambient:+.3f} K)")
        return text

    def temperature_limits(self, values: np.ndarray) -> tuple[float, float]:
        """Colour range centred on the inlet, so blue = cooler, red = warmer."""
        values = np.asarray(values, dtype=float)
        if self.ambient is None or values.size == 0:
            return float(np.nanmin(values)), float(np.nanmax(values))
        reach = float(np.nanpercentile(np.abs(values - self.ambient), 99.5))
        reach = max(reach, 0.05)
        return self.ambient - reach, self.ambient + reach


TITLES = {"temperature": "Air temperature", "velocity": "Air speed",
          "streamlines": "Streamlines", "wall_temperature": "Wall temperature"}
VIEWS = {"y": ((0, -1, 0), (0, 0, 1)), "x": ((-1, 0, 0), (0, 0, 1)), "z": ((0, 0, 1), (0, 1, 0))}
SHIELD_COLOUR = "#b8c2cf"


def _bar(title: str, compact: bool) -> dict:
    return {"title": title, "color": "#1d2433", "title_font_size": 14 if compact else 18,
            "label_font_size": 12 if compact else 15, "fmt": "%.2f", "vertical": True,
            "position_x": 0.86, "position_y": 0.12, "height": 0.72, "width": 0.05,
            "n_labels": 5, "shadow": False}


def _draw(plotter, ctx: _Context, quantity: str, compact: bool = False) -> None:
    """Draw one quantity into the active (sub)plot."""
    import pyvista as pv

    normal = PLANES[ctx.plane]
    span = ctx.span
    if quantity in ("temperature", "velocity"):
        cut = ctx.region.slice(normal=normal, origin=ctx.probe)
        if cut.n_points == 0:
            raise StudyRenderError("the cutting plane misses the solution")
        if quantity == "temperature":
            if ctx.temperature is None:
                raise StudyRenderError("this solution has no temperature")
            values = np.asarray(cut.point_data[ctx.temperature])
            plotter.add_mesh(cut, scalars=ctx.temperature, cmap="RdBu_r",
                             clim=ctx.temperature_limits(values), smooth_shading=True,
                             scalar_bar_args=_bar("Air temperature [K]", compact))
            try:
                lines = cut.contour(isosurfaces=12, scalars=ctx.temperature)
                if lines.n_points:
                    plotter.add_mesh(lines, color="#27303f", line_width=1.0, opacity=0.45)
            except Exception:  # noqa: BLE001 - isolines are decoration
                pass
        else:
            plotter.add_mesh(cut, scalars="speed", cmap="viridis", smooth_shading=True,
                             scalar_bar_args=_bar("Air speed [m/s]", compact))
            axes = [i for i in range(3) if normal[i] == 0.0]
            other = [i for i in range(3) if i not in axes][0]
            u = np.linspace(ctx.crop_low[axes[0]], ctx.crop_high[axes[0]], 30)
            v = np.linspace(ctx.crop_low[axes[1]], ctx.crop_high[axes[1]], 20)
            points = np.zeros((u.size * v.size, 3))
            points[:, axes[0]] = np.repeat(u, v.size)
            points[:, axes[1]] = np.tile(v, u.size)
            points[:, other] = ctx.probe[other]
            arrows = pv.PolyData(points).sample(ctx.grid)
            vectors = np.asarray(arrows.point_data[ctx.velocity], dtype=float)
            vectors[:, other] = 0.0
            speed = np.linalg.norm(vectors, axis=1)
            keep = speed > 1e-6
            if keep.any():
                arrows = arrows.extract_points(np.flatnonzero(keep))
                arrows.point_data["v"] = vectors[keep] / max(float(speed.max()), 1e-9)
                plotter.add_mesh(arrows.glyph(orient="v", scale="v", factor=0.11 * span),
                                 color="white", opacity=0.85)
        shell = ctx.walls.clip(normal=normal, origin=ctx.probe, invert=False)
        if shell.n_points:
            plotter.add_mesh(shell, color=SHIELD_COLOUR, smooth_shading=True,
                             specular=0.4, specular_power=20)
        view = VIEWS[ctx.plane]
    elif quantity == "streamlines":
        # A rake of seeds upstream, across the whole height and depth of the
        # object, so the lines go through every gap and round the outside.
        x0 = ctx.low[0] - 0.6 * span
        ys = np.linspace(ctx.low[1] + 0.1 * (ctx.high[1] - ctx.low[1]),
                         ctx.high[1] - 0.1 * (ctx.high[1] - ctx.low[1]), 5)
        zs = np.linspace(ctx.low[2] - 0.15 * span, ctx.high[2] + 0.15 * span, 24)
        seeds = pv.PolyData(np.array([(x0, y, z) for y in ys for z in zs]))
        lines = ctx.region.streamlines_from_source(
            seeds, vectors=ctx.velocity, max_length=12.0 * span,
            integration_direction="both",
        )
        if lines.n_points:
            scalars = ctx.temperature if (ctx.temperature and ctx.temperature in lines.point_data) else "speed"
            args = {}
            if scalars == ctx.temperature:
                args["clim"] = ctx.temperature_limits(np.asarray(lines.point_data[scalars]))
            plotter.add_mesh(
                lines.tube(radius=0.006 * span), scalars=scalars if scalars in lines.point_data else None,
                cmap="RdBu_r" if scalars == ctx.temperature else "viridis", smooth_shading=True,
                scalar_bar_args=_bar("Air temperature [K]" if scalars == ctx.temperature
                                     else "Air speed [m/s]", compact), **args)
        plotter.add_mesh(ctx.walls, color=SHIELD_COLOUR, opacity=0.35, smooth_shading=True)
        view = ((0.45, -1.0, 0.4), (0, 0, 1))
    else:  # wall_temperature
        if ctx.temperature is None or ctx.temperature not in ctx.walls.point_data:
            raise StudyRenderError("this solution has no wall temperature")
        plotter.add_mesh(ctx.walls, scalars=ctx.temperature, cmap="inferno", smooth_shading=True,
                         specular=0.3, scalar_bar_args=_bar("Wall temperature [K]", compact))
        view = ((0.6, -1.0, 0.7), (0, 0, 1))

    plotter.add_mesh(pv.Sphere(radius=0.035 * span, center=ctx.probe), color="#e53935",
                     smooth_shading=True)
    plotter.add_point_labels([ctx.probe + np.array([0.0, 0.0, 0.09 * span])], [ctx.probe_name],
                             font_size=11 if compact else 14, text_color="#1d2433",
                             shape_color="white", shape_opacity=0.75, show_points=False,
                             always_visible=True)
    if quantity != "wall_temperature":
        start = np.array([ctx.low[0] - 0.95 * span, ctx.probe[1], ctx.high[2] + 0.55 * span])
        plotter.add_mesh(pv.Arrow(start=start, direction=(1, 0, 0), scale=0.4 * span),
                         color="#2b6cb0")
        plotter.add_point_labels([start + np.array([0.2 * span, 0, 0.1 * span])], ["wind"],
                                 font_size=10 if compact else 13, text_color="#2b6cb0",
                                 shape_opacity=0.0, show_points=False, always_visible=True)
    plotter.add_text(f"{TITLES[quantity]}", position="upper_left",
                     font_size=10 if compact else 13, color="#1d2433")
    plotter.view_vector(*view)
    plotter.reset_camera()
    plotter.camera.zoom(1.25)


def render_study_point(
    study_dir: Path | str,
    kind: str,
    point: str,
    quantity: str,
    output_path: Path | str,
    plane: str = "y",
) -> Path:
    """Draw one quantity (or the 2x2 ``overview``) of one solved point as PNG."""
    import pyvista as pv

    if quantity not in QUANTITIES:
        raise StudyRenderError(f"quantity must be one of {', '.join(QUANTITIES)}")
    if plane not in PLANES:
        raise StudyRenderError("plane must be 'x', 'y' or 'z'")
    pv.OFF_SCREEN = True
    ctx = _Context(Path(study_dir), kind, point, plane)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if quantity == "overview":
        panels = ["temperature", "velocity", "streamlines", "wall_temperature"]
        plotter = pv.Plotter(off_screen=True, window_size=OVERVIEW_SIZE, shape=(2, 2),
                             border=False)
        for index, name in enumerate(panels):
            plotter.subplot(index // 2, index % 2)
            plotter.set_background("#f7f8fa", top="#dfe5ee")
            try:
                _draw(plotter, ctx, name, compact=True)
            except StudyRenderError as error:
                plotter.add_text(f"{TITLES[name]}: {error}", font_size=10, color="#1d2433")
        plotter.subplot(0, 0)
        plotter.add_text(ctx.caption(), position="lower_left", font_size=10, color="#1d2433")
    else:
        plotter = pv.Plotter(off_screen=True, window_size=SIZE)
        plotter.set_background("#f7f8fa", top="#dfe5ee")
        _draw(plotter, ctx, quantity)
        plotter.add_text(ctx.caption(), position="lower_left", font_size=11, color="#1d2433")
    try:
        plotter.enable_anti_aliasing("ssaa")
    except Exception:  # noqa: BLE001 - not every backend has it
        pass
    try:
        plotter.screenshot(str(output_path))
    finally:
        plotter.close()
    return output_path
