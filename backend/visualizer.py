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
    plotter = pv.Plotter(off_screen=True, window_size=settings.window_size())
    if settings.background:
        plotter.set_background(BACKGROUND_TOP, top=BACKGROUND_BOTTOM)
    else:
        plotter.set_background("white")
    if settings.show_axes:
        plotter.add_axes(color=TEXT_COLOUR)
    if settings.title:
        plotter.add_text(
            settings.title, position="upper_left", font_size=12, color=TEXT_COLOUR
        )
    return plotter


def _scalar_bar_arguments(title: str) -> dict:
    """Scalar-bar styling, readable against the dark background."""
    return {
        "title": title,
        "color": TEXT_COLOUR,
        "title_font_size": 20,
        "label_font_size": 16,
        "n_labels": 6,
        "fmt": "%.4g",
        "vertical": True,
        "position_x": 0.86,
        "position_y": 0.12,
        "height": 0.76,
        "width": 0.06,
    }


def apply_camera(
    plotter: pv.Plotter, dataset: pv.DataSet, settings: RenderSettings
) -> None:
    """Point the camera at the data from a named viewpoint."""
    view = settings.camera_view
    if view not in CAMERA_VIEWS:
        raise VisualizationError(
            f"unknown camera view '{view}'; choose one of "
            f"{', '.join(CAMERA_VIEWS)}"
        )
    direction, up = CAMERA_VIEWS[view]
    centre = np.array(dataset.center, dtype=float)
    diagonal = float(np.linalg.norm(np.array(dataset.bounds[1::2]) - np.array(dataset.bounds[::2])))
    distance = max(diagonal, 1.0e-6) * 1.5

    position = centre + np.array(direction, dtype=float) / math.sqrt(
        sum(component**2 for component in direction)
    ) * distance
    plotter.camera_position = [tuple(position), tuple(centre), up]
    if settings.zoom != 1.0:
        plotter.camera.zoom(settings.zoom)


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
    surface = dataset.extract_surface()

    quantity = "pressure"
    if use_coefficient:
        try:
            resolve_field(surface, "pressure_coefficient")
            quantity = "pressure_coefficient"
        except VisualizationError:
            quantity = "pressure"
    name = resolve_field(surface, quantity)

    plotter = _new_plotter(settings)
    plotter.add_mesh(
        surface,
        scalars=name,
        cmap=settings.validated_colormap(),
        clim=settings.clim,
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
    contour_levels: int = 0,
    show_body: bool = True,
) -> Path:
    """Mach-number field on a cutting plane through the shock system.

    A slice along the symmetry plane shows the bow shock, oblique shocks off
    the fins and the Prandtl-Meyer expansion around the shoulder. Optional
    iso-contour lines make individual shock angles measurable.
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

    plotter = _new_plotter(settings)
    plotter.add_mesh(
        plane,
        scalars=name,
        cmap=settings.validated_colormap(),
        clim=settings.clim,
        scalar_bar_args=_scalar_bar_arguments(
            scalar_bar_title("mach", settings.scalar_bar_title)
        ),
    )

    if contour_levels > 0:
        try:
            contours = plane.contour(contour_levels, scalars=name)
            if contours.n_points:
                plotter.add_mesh(contours, color="white", line_width=1.5)
        except Exception:  # pragma: no cover - degenerate slices
            pass

    if show_body:
        body = _extract_walls(dataset)
        if body is not None and body.n_points:
            plotter.add_mesh(body, color="#8a8f98", opacity=0.55, smooth_shading=True)

    apply_camera(plotter, plane, settings)
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


def _extract_walls(dataset: pv.DataSet) -> pv.PolyData | None:
    """Best-effort extraction of the solid body surface for context."""
    try:
        return dataset.extract_surface()
    except Exception:  # pragma: no cover - unusual datasets
        return None


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
    "streamlines",
    "thermal",
)


def render_visualization(
    solution: pv.DataSet | Path | str,
    visualization_type: str,
    output_path: Path | str,
    camera_view: str = "isometric",
    slice_normal: Sequence[float] = (0.0, 1.0, 0.0),
    colormap: str = "turbo",
    resolution: str = "4k",
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

    dataset = _coerce_dataset(solution)
    settings = RenderSettings(
        resolution=resolution,
        colormap=colormap,
        camera_view=camera_view,
        title=kwargs.pop("title", None),
        clim=kwargs.pop("clim", None),
    )

    if visualization_type == "surface_pressure":
        return render_surface_pressure(dataset, output_path, settings, **kwargs)
    if visualization_type == "mach_slice":
        return render_mach_slice(
            dataset, output_path, settings, slice_normal=slice_normal, **kwargs
        )
    if visualization_type == "streamlines":
        return render_streamlines(dataset, output_path, settings, **kwargs)
    return render_thermal(
        dataset, output_path, settings, slice_normal=slice_normal, **kwargs
    )


def _coerce_dataset(solution: pv.DataSet | Path | str) -> pv.DataSet:
    """Accept a dataset, a solution file, or a run directory."""
    if isinstance(solution, (str, Path)):
        path = Path(solution)
        if path.is_dir():
            path = find_solution_file(path)
        return load_solution(path)
    return solution
