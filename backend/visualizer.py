"""Off-screen scientific rendering of solver results.

Produces the four presentation modes the platform promises -- surface
pressure, a Mach slice through the shock system, intake streamlines and
thermal contours -- as high-resolution PNGs, plus interactive scene exports.

Everything renders off-screen so the same code serves the GUI viewport, the
MCP tool and batch sweeps. Field names differ between SU2 releases and between
solver types, so fields are located by trying a list of candidate names rather
than assuming one spelling; a missing field raises with the names that *are*
present, which is far more useful than a KeyError.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

# Off-screen mode must be selected before the first render window is created.
os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")

import pyvista as pv  # noqa: E402

from core.frames import homogeneous_solver_to_rocket  # noqa: E402

pv.OFF_SCREEN = True

# Resolutions offered to callers. "4k" is the documented default for report
# figures; the GUI uses a smaller size for responsiveness.
RESOLUTIONS: dict[str, tuple[int, int]] = {
    "preview": (960, 540),
    "hd": (1920, 1080),
    "2k": (2560, 1440),
    "4k": (3840, 2160),
}

# Fluent-style palettes.
COLORMAPS = ("turbo", "coolwarm", "viridis", "jet", "plasma", "inferno")

# Named camera positions, in the wind-tunnel frame where +X is downstream.
CAMERA_VIEWS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "isometric": ((1.0, 1.0, 1.0), (0.0, 0.0, 1.0)),
    "front": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "back": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "side": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "top": ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
    "bottom": ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0)),
    "nose_quarter": ((-1.0, -0.6, 0.35), (0.0, 0.0, 1.0)),
    "tail_quarter": ((1.0, -0.6, 0.35), (0.0, 0.0, 1.0)),
}

# The same views for a rocket shown in its own frame: nose along +Z, standing
# up the way it flies. "front" still looks at the nose from ahead of it and
# "side" still looks at the pitch plane, the one an angle of attack tilts the
# rocket in and the default Mach slice lies in.
ROCKET_CAMERA_VIEWS: dict[
    str, tuple[tuple[float, float, float], tuple[float, float, float]]
] = {
    "isometric": ((1.0, -1.0, 0.6), (0.0, 0.0, 1.0)),
    "front": ((0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
    "back": ((0.0, 0.0, -1.0), (1.0, 0.0, 0.0)),
    "side": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "top": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "bottom": ((-1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
    "nose_quarter": ((0.35, -0.6, 1.0), (0.0, 0.0, 1.0)),
    "tail_quarter": ((0.35, -0.6, -1.0), (0.0, 0.0, 1.0)),
}

# Frames a scene can be drawn in; see core.frames.
FRAMES = ("solver", "rocket")

# Margin kept around the body when a slice is cropped to it, in body lengths.
# The farfield sits five to ten lengths away, and a slice through all of it
# shows the rocket as a speck with the shock system too small to read. A
# Mach 1.3 bow shock leaves the nose at about fifty degrees, so three
# quarters of a length on each side keeps it in view along most of the body.
SLICE_BODY_MARGIN = 0.75

# Iso-Mach lines drawn over a slice. Where they bunch up is a shock; a colour
# gradient alone makes a weak one easy to miss.
MACH_CONTOUR_LEVELS = 14

# Candidate field names, most specific first, for each physical quantity.
_FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "pressure": ("Pressure", "PRESSURE", "p", "Pressure_Pa"),
    "pressure_coefficient": (
        "Pressure_Coefficient",
        "Cp",
        "C_p",
        "PRESSURE_COEFFICIENT",
    ),
    "mach": ("Mach", "MACH", "Mach_Number"),
    "temperature": ("Temperature", "TEMPERATURE", "T", "Temperature_K"),
    "density": ("Density", "DENSITY", "rho"),
    "velocity": ("Velocity", "VELOCITY", "U", "Momentum"),
    "heat_flux": ("Heat_Flux", "HEAT_FLUX", "q"),
    "skin_friction": ("Skin_Friction_Coefficient", "C_f", "Cf"),
}

# Units shown on the scalar bar for each quantity.
_FIELD_UNITS: dict[str, str] = {
    "pressure": "Pa",
    "pressure_coefficient": "-",
    "mach": "-",
    "temperature": "K",
    "density": "kg/m^3",
    "velocity": "m/s",
    "heat_flux": "W/m^2",
    "skin_friction": "-",
}

# Dark background matching the application theme.
BACKGROUND_TOP = "#1b1f27"
BACKGROUND_BOTTOM = "#0d1014"
TEXT_COLOUR = "#e8ecf1"


class VisualizationError(RuntimeError):
    """Raised when a scene cannot be built from the supplied data."""


@dataclass
class RenderSettings:
    """Presentation options shared by every visualisation mode."""

    resolution: str = "4k"
    colormap: str = "turbo"
    show_edges: bool = False
    show_axes: bool = True
    background: bool = True
    scalar_bar_title: str | None = None
    clim: tuple[float, float] | None = None
    camera_view: str = "isometric"
    zoom: float = 1.0
    title: str | None = None
    frame: str = "solver"

    def window_size(self) -> tuple[int, int]:
        """Pixel dimensions for the requested resolution."""
        if self.resolution in RESOLUTIONS:
            return RESOLUTIONS[self.resolution]
        raise VisualizationError(
            f"unknown resolution '{self.resolution}'; choose one of "
            f"{', '.join(RESOLUTIONS)}"
        )

    def validated_colormap(self) -> str:
        """The colormap name, checked against the supported palettes."""
        if self.colormap not in COLORMAPS:
            raise VisualizationError(
                f"unknown colormap '{self.colormap}'; choose one of "
                f"{', '.join(COLORMAPS)}"
            )
        return self.colormap


def available_fields(dataset: pv.DataSet) -> list[str]:
    """Names of every point and cell array on a dataset."""
    return sorted(set(dataset.point_data.keys()) | set(dataset.cell_data.keys()))


def resolve_field(dataset: pv.DataSet, quantity: str) -> str:
    """Find the array holding a physical quantity.

    Parameters
    ----------
    dataset:
        Dataset to search.
    quantity:
        Canonical quantity name, e.g. ``mach`` or ``pressure``.

    Returns
    -------
    str
        The array name present on the dataset.

    Raises
    ------
    VisualizationError
        If no candidate name matches, listing what the dataset does contain.
    """
    candidates = _FIELD_CANDIDATES.get(quantity, (quantity,))
    present = available_fields(dataset)
    lowered = {name.lower(): name for name in present}

    for candidate in candidates:
        if candidate in present:
            return candidate
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]

    raise VisualizationError(
        f"no '{quantity}' field found in the solution. Tried "
        f"{', '.join(candidates)}; the dataset contains: "
        f"{', '.join(present) if present else '(no arrays)'}"
    )


def field_units(quantity: str) -> str:
    """Unit string shown on the scalar bar."""
    return _FIELD_UNITS.get(quantity, "")


def scalar_bar_title(quantity: str, override: str | None = None) -> str:
    """A scalar-bar caption carrying the quantity name and its units."""
    if override:
        return override
    units = field_units(quantity)
    label = quantity.replace("_", " ").title()
    return f"{label} [{units}]" if units else label


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_solution(path: Path | str) -> pv.DataSet:
    """Read a solver solution file.

    Accepts anything VTK can read, which covers the ``.vtu``/``.vtm`` output
    SU2 writes. Multiblock datasets are merged into a single unstructured grid
    so downstream slicing and streamline seeding have one dataset to work on.
    """
    path = Path(path)
    if not path.is_file():
        raise VisualizationError(f"solution file not found: {path}")

    try:
        dataset = pv.read(str(path))
    except Exception as error:  # pragma: no cover - depends on the file
        raise VisualizationError(f"could not read '{path.name}': {error}") from error

    if isinstance(dataset, pv.MultiBlock):
        merged = dataset.combine()
        if merged is None or merged.n_points == 0:
            raise VisualizationError(
                f"'{path.name}' contains no usable geometry"
            )
        return merged
    return dataset


def find_solution_file(directory: Path | str) -> Path:
    """Locate the volume solution in a run directory.

    SU2 names its volume output from ``VOLUME_FILENAME``; the extension
    depends on the output format requested, so the likely candidates are
    tried in order of preference.
    """
    directory = Path(directory)
    patterns = ("flow.vtm", "flow.vtu", "flow*.vtm", "flow*.vtu", "*.vtm", "*.vtu")
    for pattern in patterns:
        matches = sorted(directory.glob(pattern))
        if matches:
            return matches[0]
    raise VisualizationError(
        f"no volume solution (.vtm/.vtu) found in {directory}"
    )


# ---------------------------------------------------------------------------
# Plotter construction
# ---------------------------------------------------------------------------


def _new_plotter(settings: RenderSettings) -> pv.Plotter:
    """Create an off-screen plotter styled for the application theme."""
    global _CURRENT_WINDOW
    _CURRENT_WINDOW = settings.window_size()
    plotter = pv.Plotter(off_screen=True, window_size=settings.window_size())
    if settings.background:
        plotter.set_background(BACKGROUND_TOP, top=BACKGROUND_BOTTOM)
    else:
        plotter.set_background("white")
    if settings.show_axes:
        plotter.add_axes(color=TEXT_COLOUR)
    if settings.title:
        plotter.add_text(
            settings.title,
            position="upper_left",
            font_size=int(round(CAPTION_SIZE * _font_scale() * 0.6)),
            color=TEXT_COLOUR,
        )
    return plotter


# Legend and caption sizes for a 1080-pixel-high image; larger images scale
# them up. They used to be fixed, and on a 4K render the legend came out a
# quarter of the height it should have been -- too small to read.
LEGEND_TITLE_SIZE = 30
LEGEND_LABEL_SIZE = 24
CAPTION_SIZE = 22


def _font_scale() -> float:
    """How much larger than a 1080-pixel image the one being drawn is."""
    height = _CURRENT_WINDOW[1] if _CURRENT_WINDOW else 1080
    return max(0.6, height / 1080.0)


# Set by _new_plotter so the legend helper knows the image size.
_CURRENT_WINDOW: tuple[int, int] | None = None


def _scalar_bar_arguments(title: str) -> dict:
    """Scalar-bar styling, readable against the dark background at any size."""
    scale = _font_scale()
    return {
        "title": title,
        "color": TEXT_COLOUR,
        "title_font_size": int(round(LEGEND_TITLE_SIZE * scale)),
        "label_font_size": int(round(LEGEND_LABEL_SIZE * scale)),
        "n_labels": 6,
        "fmt": "%.4g",
        "vertical": True,
        # Without this VTK shrinks the text to fit the bar's width, whatever
        # size was asked for -- the other half of why it was unreadable.
        "unconstrained_font_size": True,
        "position_x": 0.84,
        "position_y": 0.12,
        "height": 0.76,
        "width": 0.06,
    }


def apply_camera(
    plotter: pv.Plotter,
    dataset: pv.DataSet,
    settings: RenderSettings,
    face_normal: Sequence[float] | None = None,
) -> None:
    """Point the camera at the data from a named viewpoint.

    ``face_normal`` is for flat things -- a slice. A plane seen from an
    angle is a skewed parallelogram with the field squashed across it, so
    the camera looks straight at it from whichever side the named view is
    on, and keeps that view's notion of up.
    """
    view = settings.camera_view
    views = ROCKET_CAMERA_VIEWS if settings.frame == "rocket" else CAMERA_VIEWS
    if view not in views:
        raise VisualizationError(
            f"unknown camera view '{view}'; choose one of "
            f"{', '.join(views)}"
        )
    direction, up = views[view]
    direction = np.array(direction, dtype=float)
    direction /= np.linalg.norm(direction)
    if face_normal is not None:
        normal = np.array(face_normal, dtype=float)
        if np.linalg.norm(normal) > 0.0:
            normal /= np.linalg.norm(normal)
            direction = normal if np.dot(normal, direction) >= 0.0 else -normal
    up_vector = np.array(up, dtype=float)
    if abs(np.dot(up_vector, direction)) > 0.99 * np.linalg.norm(up_vector):
        # Looking along "up": take the body axis instead.
        up_vector = np.array([0.0, 0.0, 1.0] if settings.frame == "rocket" else [1.0, 0.0, 0.0])
        if abs(np.dot(up_vector, direction)) > 0.99:
            up_vector = np.array([0.0, 1.0, 0.0])
    # Up must be perpendicular to the view direction for the fit below.
    up_vector = up_vector - np.dot(up_vector, direction) * direction
    if np.linalg.norm(up_vector) < 1.0e-9:  # pragma: no cover - degenerate view
        up_vector = np.array([0.0, 0.0, 1.0])
    up_vector /= np.linalg.norm(up_vector)
    right = np.cross(direction, up_vector)

    low = np.array(dataset.bounds[::2], dtype=float)
    high = np.array(dataset.bounds[1::2], dtype=float)
    centre = 0.5 * (low + high)
    corners = np.array(
        [[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])]
    ) - centre
    half_width = float(np.max(np.abs(corners @ right)))
    half_height = float(np.max(np.abs(corners @ up_vector)))
    depth = float(np.max(np.abs(corners @ direction)))

    # Parallel projection, fitted to what the model actually covers on
    # screen. The old placement stood the camera 1.5 diagonals away with a
    # 30-degree perspective lens, which for a 1.3 m rocket standing upright
    # in a wide image cut off the nose and the tail. The legend takes the
    # right-hand sixth of the frame, so only the rest is counted as usable.
    width, height = settings.window_size()
    aspect = (width / height) * USABLE_WIDTH_FRACTION
    scale = max(half_height, half_width / aspect, 1.0e-6) * FRAME_MARGIN
    distance = max(depth * 3.0, scale * 4.0, 1.0e-3)
    # Shift left by half the legend's share, so the model is centred in the
    # space it has rather than under the legend.
    shift = right * scale * (width / height) * (1.0 - USABLE_WIDTH_FRACTION)
    # ``direction`` points from the model to the camera, so ``right`` as
    # computed is screen-left; moving the focal point that way moves the
    # model right on screen -- under the legend. Hence the minus.
    focal = centre - shift
    plotter.camera_position = [
        tuple(focal + direction * distance),
        tuple(focal),
        tuple(up_vector),
    ]
    plotter.enable_parallel_projection()
    plotter.camera.parallel_scale = scale / max(settings.zoom, 1.0e-6)
    plotter.camera.clipping_range = (1.0e-4 * distance, distance * 4.0 + depth * 4.0)


# Share of the image width the model may use; the scalar bar has the rest.
USABLE_WIDTH_FRACTION = 0.78
# Breathing room around the model.
FRAME_MARGIN = 1.08


def _save(plotter: pv.Plotter, output_path: Path) -> Path:
    """Render and write the PNG, then release the render window."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        plotter.screenshot(str(output_path))
    finally:
        plotter.close()
    if not output_path.is_file():  # pragma: no cover - renderer failure
        raise VisualizationError(f"renderer produced no file at {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# Visualisation modes
# ---------------------------------------------------------------------------


def render_surface_pressure(
    dataset: pv.DataSet,
    output_path: Path | str,
    settings: RenderSettings | None = None,
    use_coefficient: bool = True,
) -> Path:
    """Surface pressure or pressure-coefficient map on the body.

    Prefers the pressure coefficient when the solver wrote one, since C_p is
    the comparable quantity across flight conditions; falls back to absolute
    pressure otherwise.
    """
    settings = settings or RenderSettings()
    # The body, not the whole boundary: the volume's outer surface includes
    # the farfield, which would otherwise fill the frame with a cylinder of
    # freestream pressure and leave the rocket a speck inside it.
    surface = _extract_walls(dataset)
    if surface is None or surface.n_points == 0:
        surface = dataset.extract_surface()

    quantity = "pressure"
    if use_coefficient:
        try:
            resolve_field(surface, "pressure_coefficient")
            quantity = "pressure_coefficient"
        except VisualizationError:
            quantity = "pressure"
    name = resolve_field(surface, quantity)
    # By area, like the Mach slice: the stagnation point at the nose tip is
    # a few square millimetres at C_p 2.2, and taking the range from it
    # painted the whole body one shade of blue.
    clim = settings.clim or _area_weighted_range(surface, name, 1.0, 99.0, floor=None)

    plotter = _new_plotter(settings)
    plotter.add_mesh(
        surface,
        scalars=name,
        cmap=settings.validated_colormap(),
        clim=clim,
        show_edges=settings.show_edges,
        smooth_shading=True,
        scalar_bar_args=_scalar_bar_arguments(
            scalar_bar_title(quantity, settings.scalar_bar_title)
        ),
    )
    apply_camera(plotter, surface, settings)
    return _save(plotter, Path(output_path))


def render_mach_slice(
    dataset: pv.DataSet,
    output_path: Path | str,
    settings: RenderSettings | None = None,
    slice_normal: Sequence[float] = (0.0, 1.0, 0.0),
    slice_origin: Sequence[float] | None = None,
    contour_levels: int = MACH_CONTOUR_LEVELS,
    show_body: bool = True,
    crop_margin: float | None = SLICE_BODY_MARGIN,
) -> Path:
    """Mach-number field on a cutting plane through the shock system.

    A slice along the symmetry plane shows the bow shock, oblique shocks off
    the fins and the Prandtl-Meyer expansion around the shoulder. Optional
    iso-contour lines make individual shock angles measurable.

    ``crop_margin`` trims the plane to the body plus that many body lengths
    on every side; ``None`` shows the whole domain.
    """
    settings = settings or RenderSettings()
    name = resolve_field(dataset, "mach")

    origin = (
        np.array(slice_origin, dtype=float)
        if slice_origin is not None
        else np.array(dataset.center, dtype=float)
    )
    normal = np.array(slice_normal, dtype=float)
    if np.linalg.norm(normal) < 1.0e-12:
        raise VisualizationError("slice normal must be a non-zero vector")

    plane = dataset.slice(normal=tuple(normal), origin=tuple(origin))
    if plane.n_points == 0:
        raise VisualizationError(
            "the cutting plane does not intersect the solution domain"
        )

    body = _extract_walls(dataset)
    if crop_margin is not None and body is not None and body.n_points:
        plane = _crop_to_body(plane, body, crop_margin)

    clim = settings.clim or _area_weighted_range(plane, name)

    plotter = _new_plotter(settings)
    plotter.add_mesh(
        plane,
        scalars=name,
        cmap=settings.validated_colormap(),
        clim=clim,
        scalar_bar_args=_scalar_bar_arguments(
            scalar_bar_title("mach", settings.scalar_bar_title)
        ),
    )

    if contour_levels > 0:
        try:
            levels = np.linspace(clim[0], clim[1], contour_levels + 2)[1:-1] if clim else contour_levels
            contours = plane.contour(levels, scalars=name)
            if contours.n_points:
                plotter.add_mesh(
                    contours, color="white", line_width=1.0, opacity=0.45
                )
        except Exception:  # pragma: no cover - degenerate slices
            pass

    if show_body and body is not None and body.n_points:
        plotter.add_mesh(body, color="#8a8f98", opacity=0.55, smooth_shading=True)

    apply_camera(plotter, plane, settings, face_normal=slice_normal)
    return _save(plotter, Path(output_path))


def render_schlieren(
    dataset: pv.DataSet,
    output_path: Path | str,
    settings: RenderSettings | None = None,
    slice_normal: Sequence[float] = (0.0, 1.0, 0.0),
    slice_origin: Sequence[float] | None = None,
    show_body: bool = True,
    crop_margin: float | None = SLICE_BODY_MARGIN,
) -> Path:
    """Numerical schlieren: the density gradient on a cutting plane.

    What a wind-tunnel schlieren photograph shows, and the clearest picture
    of a shock system there is. On a slender rocket at low supersonic speed
    the shocks are weak -- Mach falls by a few hundredths across the nose
    shock -- and a Mach-number colour map barely shows them; the density
    gradient jumps by orders of magnitude across any shock, however weak.
    Drawn on a logarithmic grey scale, dark where the gradient is strong.
    """
    settings = settings or RenderSettings()
    name = resolve_field(dataset, "density")

    origin = (
        np.array(slice_origin, dtype=float)
        if slice_origin is not None
        else np.array(dataset.center, dtype=float)
    )
    plane = dataset.slice(normal=tuple(slice_normal), origin=tuple(origin))
    if plane.n_points == 0:
        raise VisualizationError(
            "the cutting plane does not intersect the solution domain"
        )
    body = _extract_walls(dataset)
    if crop_margin is not None and body is not None and body.n_points:
        plane = _crop_to_body(plane, body, crop_margin)

    derived = plane.compute_derivative(scalars=name, gradient="gradient")
    gradient = np.linalg.norm(np.asarray(derived.point_data["gradient"]), axis=1)
    floor = max(float(np.percentile(gradient, 5)), 1.0e-12)
    derived.point_data["schlieren"] = np.log10(np.maximum(gradient, floor))
    finite = derived.point_data["schlieren"]
    clim = settings.clim or (
        float(np.percentile(finite, 5)),
        float(np.percentile(finite, 99.5)),
    )

    plotter = _new_plotter(settings)
    plotter.add_mesh(
        derived,
        scalars="schlieren",
        cmap="gray_r",
        clim=clim,
        scalar_bar_args=_scalar_bar_arguments(
            settings.scalar_bar_title or "log10 |grad rho|"
        ),
    )
    if show_body and body is not None and body.n_points:
        plotter.add_mesh(body, color="#4da3ff", smooth_shading=True)
    apply_camera(plotter, derived, settings, face_normal=slice_normal)
    return _save(plotter, Path(output_path))


def render_streamlines(
    dataset: pv.DataSet,
    output_path: Path | str,
    settings: RenderSettings | None = None,
    seed_center: Sequence[float] | None = None,
    seed_radius: float | None = None,
    n_points: int = 300,
    colour_by: str = "velocity",
    tube_radius: float | None = None,
    show_body: bool = True,
) -> Path:
    """Streamlines seeded at the intake, coloured by velocity or temperature.

    This is the sensor-microclimate view: it shows how much of the air
    entering the sampling tube has been warmed by the irradiated roof before
    it reaches the die.
    """
    settings = settings or RenderSettings()
    velocity_name = resolve_field(dataset, "velocity")

    body = _extract_walls(dataset) if show_body else None
    if seed_center is None and body is not None and body.n_points:
        return _render_body_streamlines(
            dataset, body, velocity_name, output_path, settings, n_points, colour_by
        )

    bounds = np.array(dataset.bounds, dtype=float)
    extent = bounds[1::2] - bounds[::2]
    centre = (
        np.array(seed_center, dtype=float)
        if seed_center is not None
        else np.array(dataset.center, dtype=float)
    )
    radius = (
        float(seed_radius)
        if seed_radius is not None
        else 0.05 * float(np.linalg.norm(extent))
    )

    arguments = {
        "vectors": velocity_name,
        "source_center": tuple(centre),
        "source_radius": radius,
        "n_points": n_points,
        "integration_direction": "both",
    }
    span = float(np.linalg.norm(extent)) * 5.0

    try:
        # PyVista renamed this parameter; accept either so the module works
        # across the versions likely to be installed.
        try:
            streamlines = dataset.streamlines(max_length=span, **arguments)
        except TypeError:
            streamlines = dataset.streamlines(max_time=span, **arguments)
    except Exception as error:
        raise VisualizationError(
            f"streamline integration failed: {error}"
        ) from error

    if streamlines.n_points == 0:
        raise VisualizationError(
            "no streamlines were produced; check the seed location lies inside "
            "the fluid domain"
        )

    colour_field = resolve_field(streamlines, colour_by)
    geometry = streamlines
    if tube_radius is None:
        tube_radius = 0.0015 * float(np.linalg.norm(extent))
    if tube_radius > 0.0:
        try:
            geometry = streamlines.tube(radius=tube_radius)
        except Exception:  # pragma: no cover - degenerate lines
            geometry = streamlines

    plotter = _new_plotter(settings)
    plotter.add_mesh(
        geometry,
        scalars=colour_field,
        cmap=settings.validated_colormap(),
        clim=settings.clim,
        scalar_bar_args=_scalar_bar_arguments(
            scalar_bar_title(colour_by, settings.scalar_bar_title)
        ),
    )

    if show_body:
        body = _extract_walls(dataset)
        if body is not None and body.n_points:
            plotter.add_mesh(body, color="#8a8f98", opacity=0.35, smooth_shading=True)

    apply_camera(plotter, dataset, settings)
    return _save(plotter, Path(output_path))


# Streamline seeding around a body: a disc this many body radii across,
# this far upstream of the nose in body lengths, and the lines cropped to
# this margin around the body.
STREAMLINE_SEED_RADII = 2.2
STREAMLINE_UPSTREAM = 0.15
STREAMLINE_MARGIN = 0.35
# Seeds in the rake.
STREAMLINE_RAKE_COUNT = 31


def _screen_right(settings: RenderSettings, flow: np.ndarray) -> np.ndarray:
    """A direction across the flow that lies across the picture."""
    views = ROCKET_CAMERA_VIEWS if settings.frame == "rocket" else CAMERA_VIEWS
    direction, up = views.get(settings.camera_view, views["isometric"])
    right = np.cross(np.array(up, dtype=float), np.array(direction, dtype=float))
    right = right - np.dot(right, flow) * flow
    if np.linalg.norm(right) < 1.0e-9:
        right = np.cross(flow, [0.0, 0.0, 1.0])
        if np.linalg.norm(right) < 1.0e-9:
            right = np.cross(flow, [1.0, 0.0, 0.0])
    return right / np.linalg.norm(right)


def _render_body_streamlines(
    dataset: pv.DataSet,
    body: pv.DataSet,
    velocity_name: str,
    output_path: Path | str,
    settings: RenderSettings,
    n_points: int,
    colour_by: str,
) -> Path:
    """Flow paths past a body, seeded just upstream of it.

    Seeding at the middle of the domain -- what this did before -- put the
    seeds in open air beside a slender rocket, and every line came out
    straight and the same colour: a picture of the freestream. Here the
    seeds are a disc just ahead of the nose, slightly wider than the fins,
    so each line passes the body, and the picture is cropped to the body.
    """
    velocity = np.asarray(dataset.point_data[velocity_name], dtype=float)
    flow = np.median(velocity, axis=0)
    if np.linalg.norm(flow) < 1.0e-12:
        raise VisualizationError("the velocity field is zero everywhere")
    flow = flow / np.linalg.norm(flow)

    low = np.array(body.bounds[::2], dtype=float)
    high = np.array(body.bounds[1::2], dtype=float)
    centre = 0.5 * (low + high)
    corners = np.array(
        [[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])]
    ) - centre
    length = float(np.ptp(corners @ flow))
    lateral = corners - np.outer(corners @ flow, flow)
    radius = float(np.max(np.linalg.norm(lateral, axis=1))) or 0.05 * length

    upstream = centre - flow * (0.5 * length + STREAMLINE_UPSTREAM * length)
    # A rake of seeds across the flow in the plane the camera looks at, like
    # smoke filaments in a wind tunnel: a full disc of seeds buried the
    # rocket in a wall of lines.
    across = _screen_right(settings, flow)
    count = max(9, min(STREAMLINE_RAKE_COUNT, n_points))
    offsets = np.linspace(-STREAMLINE_SEED_RADII * radius, STREAMLINE_SEED_RADII * radius, count)
    seeds = pv.PolyData(upstream + np.outer(offsets, across))

    span = float(np.linalg.norm(np.array(dataset.bounds[1::2]) - np.array(dataset.bounds[::2])))
    try:
        lines = dataset.streamlines_from_source(
            seeds,
            vectors=velocity_name,
            integration_direction="forward",
            max_length=span,
        )
    except TypeError:  # pragma: no cover - older PyVista
        lines = dataset.streamlines_from_source(
            seeds, vectors=velocity_name, integration_direction="forward", max_time=span
        )
    if lines.n_points == 0:
        raise VisualizationError("no streamlines could be traced past the body")
    lines = _crop_to_body(lines, body, STREAMLINE_MARGIN)

    try:
        colour_field = resolve_field(lines, "mach" if colour_by == "velocity" else colour_by)
        quantity = "mach" if colour_by == "velocity" else colour_by
    except VisualizationError:
        colour_field = resolve_field(lines, colour_by)
        quantity = colour_by
    values = np.asarray(lines.point_data[colour_field], dtype=float)
    if values.ndim > 1:
        values = np.linalg.norm(values, axis=1)
        lines.point_data["_colour"] = values
        colour_field = "_colour"
    clim = settings.clim or (
        float(np.percentile(values, 2)),
        float(np.percentile(values, 99.5)),
    )
    if not clim[1] > clim[0]:
        clim = None

    geometry = lines
    try:
        geometry = lines.tube(radius=0.03 * radius)
    except Exception:  # pragma: no cover - degenerate lines
        pass

    plotter = _new_plotter(settings)
    plotter.add_mesh(
        geometry,
        scalars=colour_field,
        cmap=settings.validated_colormap(),
        clim=clim,
        smooth_shading=True,
        scalar_bar_args=_scalar_bar_arguments(
            scalar_bar_title(quantity, settings.scalar_bar_title)
        ),
    )
    plotter.add_mesh(body, color="#c8ccd4", smooth_shading=True)
    apply_camera(plotter, geometry, settings)
    return _save(plotter, Path(output_path))


def render_thermal(
    dataset: pv.DataSet,
    output_path: Path | str,
    settings: RenderSettings | None = None,
    slice_normal: Sequence[float] | None = (0.0, 1.0, 0.0),
    slice_origin: Sequence[float] | None = None,
    probe_point: Sequence[float] | None = None,
    probe_label: str = "BMP580",
) -> Path:
    """Temperature field through the enclosure, with the sensor marked.

    Defaults to ``coolwarm``, the conventional diverging palette for
    temperature, unless the caller chose otherwise.
    """
    settings = settings or RenderSettings(colormap="coolwarm")
    name = resolve_field(dataset, "temperature")

    plotter = _new_plotter(settings)
    scalar_bar = _scalar_bar_arguments(
        scalar_bar_title("temperature", settings.scalar_bar_title)
    )

    if slice_normal is not None:
        origin = (
            np.array(slice_origin, dtype=float)
            if slice_origin is not None
            else np.array(dataset.center, dtype=float)
        )
        plane = dataset.slice(normal=tuple(slice_normal), origin=tuple(origin))
        if plane.n_points == 0:
            raise VisualizationError(
                "the cutting plane does not intersect the solution domain"
            )
        plotter.add_mesh(
            plane,
            scalars=name,
            cmap=settings.validated_colormap(),
            clim=settings.clim,
            scalar_bar_args=scalar_bar,
        )
        surface = dataset.extract_surface()
        plotter.add_mesh(surface, color="#7d828b", opacity=0.25)
        focus = plane
    else:
        surface = dataset.extract_surface()
        plotter.add_mesh(
            surface,
            scalars=name,
            cmap=settings.validated_colormap(),
            clim=settings.clim,
            scalar_bar_args=scalar_bar,
        )
        focus = surface

    if probe_point is not None:
        point = np.array(probe_point, dtype=float).reshape(1, 3)
        extent = np.array(dataset.bounds[1::2]) - np.array(dataset.bounds[::2])
        marker_radius = 0.01 * float(np.linalg.norm(extent))
        plotter.add_mesh(
            pv.Sphere(radius=max(marker_radius, 1e-6), center=point[0]),
            color="#ffdd55",
        )
        plotter.add_point_labels(
            point,
            [probe_label],
            font_size=16,
            text_color=TEXT_COLOUR,
            point_size=1,
            shape_opacity=0.0,
        )

    apply_camera(plotter, focus, settings)
    return _save(plotter, Path(output_path))


def _area_weighted_range(
    plane: pv.DataSet,
    name: str,
    low: float = 2.0,
    high: float = 98.0,
    floor: float | None = 0.6,
) -> tuple[float, float] | None:
    """The colour range that covers most of the picture, by area.

    Min to max is dominated by the stagnation zero at the nose and in the
    base wake, which squeezes the whole shock system -- Mach 1.3 against
    1.15 behind a slender-body shock -- into two neighbouring shades of red.
    Percentiles are taken by area rather than by point, because points crowd
    into the boundary layer where the flow is slowest.
    """
    try:
        cells = plane.point_data_to_cell_data()
        values = np.asarray(cells.cell_data[name], dtype=float)
        areas = np.asarray(
            plane.compute_cell_sizes(length=False, volume=False).cell_data["Area"],
            dtype=float,
        )
    except Exception:  # pragma: no cover - unusual datasets
        return None
    finite = np.isfinite(values) & (areas > 0.0)
    if np.count_nonzero(finite) < 2:
        return None
    values, areas = values[finite], areas[finite]
    order = np.argsort(values)
    cumulative = np.cumsum(areas[order]) / areas.sum()
    lower = float(values[order][np.searchsorted(cumulative, low / 100.0)])
    upper = float(values[order][min(np.searchsorted(cumulative, high / 100.0), values.size - 1)])
    if not upper > lower:
        return None
    # Freestream fills most of the area, so the low percentile sits close to
    # it; keeping the range at least 40% of the top value deep leaves room
    # for the flow behind a shock and around the body to read as colour.
    if floor is None:
        return lower, upper
    return min(lower, floor * upper), upper


def _extract_walls(dataset: pv.DataSet) -> pv.PolyData | None:
    """The solid body's surface, without the farfield around it.

    The boundary of a flow volume is two disconnected pieces: the body, and
    the farfield enclosing everything. The farfield is the piece that spans
    the whole domain, so it is recognised by its size and dropped. A
    dataset whose boundary is a single piece has no body inside it, and
    ``None`` is returned rather than the domain's own outer skin -- drawing
    that translucent over a slice washes the whole picture out.
    """
    try:
        surface = dataset.extract_surface()
        if surface.n_cells == 0:
            return None
        regions = surface.connectivity("all")
    except Exception:  # pragma: no cover - unusual datasets
        return None

    labels = np.asarray(regions.cell_data.get("RegionId", []))
    if labels.size == 0:
        return None
    region_ids = np.unique(labels)
    if region_ids.size < 2:
        return None

    def diagonal(region: int) -> float:
        cells = regions.extract_cells(np.flatnonzero(labels == region))
        low, high = np.array(cells.bounds[::2]), np.array(cells.bounds[1::2])
        return float(np.linalg.norm(high - low))

    # Every piece about as large as the domain is farfield. There can be
    # more than one: SU2's multiblock output carries the farfield both as the
    # volume's outer skin and as its own boundary block, and dropping only
    # the largest piece left the other drawn translucent over every slice
    # and stretched the crop box to the whole domain.
    sizes = {region: diagonal(region) for region in region_ids}
    largest = max(sizes.values())
    bodies = [region for region, size in sizes.items() if size < 0.5 * largest]
    if not bodies:
        return None
    walls = regions.extract_cells(np.flatnonzero(np.isin(labels, bodies)))
    return walls.extract_surface()


def _crop_to_body(
    plane: pv.DataSet, body: pv.DataSet, margin_lengths: float
) -> pv.DataSet:
    """Trim a slice to the body plus a margin, in body lengths."""
    low = np.array(body.bounds[::2], dtype=float)
    high = np.array(body.bounds[1::2], dtype=float)
    length = float(np.max(high - low))
    if length <= 0.0:
        return plane
    pad = margin_lengths * length
    box = [
        value
        for pair in zip(low - pad, high + pad)
        for value in pair
    ]
    try:
        cropped = plane.clip_box(box, invert=False)
    except Exception:  # pragma: no cover - degenerate geometry
        return plane
    return cropped if cropped.n_points else plane


# ---------------------------------------------------------------------------
# Probing and export
# ---------------------------------------------------------------------------


def probe_point_value(
    dataset: pv.DataSet, point: Sequence[float], quantity: str
) -> float:
    """Interpolate a field at one coordinate.

    This is how the sensor temperature is read out at the BMP580 die
    location.

    Raises
    ------
    VisualizationError
        If the point lies outside the dataset, where interpolation would
        silently return zero.
    """
    name = resolve_field(dataset, quantity)
    probe = pv.PolyData(np.array(point, dtype=float).reshape(1, 3))
    sampled = probe.sample(dataset)

    valid = sampled.point_data.get("vtkValidPointMask")
    if valid is not None and not bool(np.asarray(valid).ravel()[0]):
        raise VisualizationError(
            f"point {list(point)} lies outside the solution domain "
            f"{tuple(round(b, 4) for b in dataset.bounds)}"
        )

    values = np.asarray(sampled.point_data[name]).ravel()
    if values.size == 0:
        raise VisualizationError(f"probing '{name}' returned no value")
    return float(values[0])


def export_scene(
    dataset: pv.DataSet, output_path: Path | str, fmt: str | None = None
) -> Path:
    """Write the dataset for interactive viewing.

    Supports ``.vtk``/``.vtu``/``.vtm`` for analysis tools and ``.gltf`` for
    sharing a 3D scene that opens in a browser.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = (fmt or output_path.suffix.lstrip(".")).lower()

    if suffix in ("gltf", "glb"):
        plotter = pv.Plotter(off_screen=True)
        plotter.add_mesh(dataset.extract_surface())
        try:
            plotter.export_gltf(str(output_path))
        finally:
            plotter.close()
    elif suffix in ("vtk", "vtu", "vtm", "vtp"):
        dataset.save(str(output_path))
    else:
        raise VisualizationError(
            f"unsupported export format '{suffix}'; use gltf, vtk, vtu or vtp"
        )

    if not output_path.is_file():  # pragma: no cover - writer failure
        raise VisualizationError(f"export produced no file at {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# Dispatcher used by the MCP tool and the GUI
# ---------------------------------------------------------------------------

VISUALIZATION_TYPES = (
    "surface_pressure",
    "mach_slice",
    "schlieren",
    "streamlines",
    "thermal",
)


# The solver configs a run directory may hold, the one that ran last first:
# a rescued run's final stage is what the solution came from.
_CONFIG_NAMES = ("solver_stage_b.cfg", "solver.cfg")


def flow_condition(directory: Path | str) -> dict[str, float] | None:
    """Mach, speed, angle of attack and sideslip a run was solved at.

    Read from the solver configuration written next to the solution, so it
    works for single runs and sweep points alike, whatever recorded them.
    """
    directory = Path(directory)
    if directory.is_file():
        directory = directory.parent
    for name in _CONFIG_NAMES:
        path = directory / name
        if not path.is_file():
            continue
        values: dict[str, float] = {}
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                key, _, value = line.partition("=")
                key = key.strip()
                if key in ("MACH_NUMBER", "AOA", "SIDESLIP_ANGLE", "FREESTREAM_TEMPERATURE"):
                    try:
                        values[key] = float(value.split("%")[0].strip())
                    except ValueError:
                        pass
        except OSError:
            return None
        if "MACH_NUMBER" not in values:
            return None
        mach = values["MACH_NUMBER"]
        temperature = values.get("FREESTREAM_TEMPERATURE", 288.15)
        return {
            "mach": mach,
            "speed_ms": mach * math.sqrt(1.4 * 287.05 * temperature),
            "aoa_deg": values.get("AOA", 0.0),
            "sideslip_deg": values.get("SIDESLIP_ANGLE", 0.0),
        }
    return None


def flow_caption(directory: Path | str) -> str:
    """'Mach 0.50 (170 m/s)  |  AoA 7°  |  sideslip 0°', or '' if unknown."""
    condition = flow_condition(directory)
    if condition is None:
        return ""
    return (
        f"Mach {condition['mach']:.2f} ({condition['speed_ms']:.0f} m/s)  |  "
        f"AoA {condition['aoa_deg']:g}°  |  sideslip {condition['sideslip_deg']:g}°"
    )


def render_visualization(
    solution: pv.DataSet | Path | str,
    visualization_type: str,
    output_path: Path | str,
    camera_view: str = "isometric",
    slice_normal: Sequence[float] = (0.0, 1.0, 0.0),
    colormap: str = "turbo",
    resolution: str = "4k",
    frame: str = "solver",
    **kwargs,
) -> Path:
    """Render one of the four named visualisation modes.

    Parameters
    ----------
    solution:
        A loaded dataset, or a path to a solution file or run directory.
    visualization_type:
        One of ``surface_pressure``, ``mach_slice``, ``streamlines``,
        ``thermal``.
    output_path:
        Destination PNG.
    camera_view:
        Named viewpoint, e.g. ``isometric`` or ``nose_quarter``.
    slice_normal:
        Cutting-plane normal for the slice modes.
    colormap, resolution:
        Presentation options.
    frame:
        ``"rocket"`` stands a rocket on its tail with the nose along +Z, as
        the operator sees it everywhere else; ``"solver"`` draws the raw
        solver frame, body along +X. ``slice_normal`` and the camera views
        are read in whichever frame is chosen.

    Returns
    -------
    Path
        The written image path.
    """
    if visualization_type not in VISUALIZATION_TYPES:
        raise VisualizationError(
            f"unknown visualization type '{visualization_type}'; choose one of "
            f"{', '.join(VISUALIZATION_TYPES)}"
        )

    if frame not in FRAMES:
        raise VisualizationError(
            f"unknown frame '{frame}'; choose one of {', '.join(FRAMES)}"
        )

    title = kwargs.pop("title", None)
    if isinstance(solution, (str, Path)):
        # Every picture says which case it is: an exported image of a sweep
        # point is otherwise indistinguishable from its neighbours.
        condition = flow_caption(solution)
        if condition:
            title = f"{title}\n{condition}" if title else condition

    dataset = _coerce_dataset(solution)
    if frame == "rocket":
        dataset = to_rocket_frame(dataset)
    settings = RenderSettings(
        resolution=resolution,
        colormap=colormap,
        camera_view=camera_view,
        title=title,
        clim=kwargs.pop("clim", None),
        frame=frame,
    )

    if visualization_type == "surface_pressure":
        return render_surface_pressure(dataset, output_path, settings, **kwargs)
    if visualization_type == "mach_slice":
        return render_mach_slice(
            dataset, output_path, settings, slice_normal=slice_normal, **kwargs
        )
    if visualization_type == "schlieren":
        return render_schlieren(
            dataset, output_path, settings, slice_normal=slice_normal, **kwargs
        )
    if visualization_type == "streamlines":
        return render_streamlines(dataset, output_path, settings, **kwargs)
    return render_thermal(
        dataset, output_path, settings, slice_normal=slice_normal, **kwargs
    )


def to_rocket_frame(dataset: pv.DataSet) -> pv.DataSet:
    """A copy of a solver-frame dataset stood up with the nose along +Z.

    Vector fields are rotated with the points, so velocity still points the
    way the air moves -- downwards, past a rocket climbing nose-first.
    """
    # Voxels and pixels are axis-aligned by definition; rotated, VTK can no
    # longer find a point inside them and streamline tracing silently finds
    # nothing. SU2 writes tetrahedra and prisms, but an image-derived grid
    # does not, so those are split first.
    celltypes = getattr(dataset, "celltypes", None)
    if celltypes is not None and np.isin(celltypes, (8, 11)).any():
        dataset = dataset.triangulate()
    return dataset.transform(
        homogeneous_solver_to_rocket(),
        transform_all_input_vectors=True,
        inplace=False,
    )


def _coerce_dataset(solution: pv.DataSet | Path | str) -> pv.DataSet:
    """Accept a dataset, a solution file, or a run directory."""
    if isinstance(solution, (str, Path)):
        path = Path(solution)
        if path.is_dir():
            path = find_solution_file(path)
        return load_solution(path)
    return solution
