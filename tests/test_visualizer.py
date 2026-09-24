"""Off-screen rendering of solver results.

Every render is checked for actual content: a plotter that fails silently
produces a uniform image, which would pass a mere "file exists" assertion. The
colour-variety check is what distinguishes a real plot from a blank canvas.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pv = pytest.importorskip("pyvista")

from backend.visualizer import (  # noqa: E402
    CAMERA_VIEWS,
    COLORMAPS,
    RESOLUTIONS,
    RenderSettings,
    VisualizationError,
    available_fields,
    export_scene,
    field_units,
    find_solution_file,
    load_solution,
    probe_point_value,
    render_mach_slice,
    render_streamlines,
    render_surface_pressure,
    render_thermal,
    render_visualization,
    resolve_field,
    scalar_bar_title,
)


# ---------------------------------------------------------------------------
# Synthetic solution data
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def flow_solution() -> pv.DataSet:
    """A structured grid carrying the fields a SU2 flow solution would.

    The Mach field contains a sharp streamwise jump so a slice has genuine
    structure to render rather than a smooth gradient.
    """
    grid = pv.ImageData(dimensions=(24, 18, 18), spacing=(0.05, 0.02, 0.02))
    grid.origin = (-0.2, -0.18, -0.18)
    points = grid.points

    radius = np.linalg.norm(points[:, 1:], axis=1)
    x = points[:, 0]

    mach = np.where(x < 0.3, 2.5, 1.2) + 0.3 * np.tanh(10.0 * (radius - 0.05))
    pressure = 101325.0 * (1.0 + 0.5 * np.exp(-40.0 * radius**2) - 0.2 * np.tanh(x))
    temperature = 288.15 + 60.0 * np.exp(-30.0 * radius**2) + 15.0 * x

    velocity = np.zeros_like(points)
    velocity[:, 0] = 200.0 + 40.0 * np.tanh(5.0 * x)
    velocity[:, 1] = 5.0 * points[:, 1]
    velocity[:, 2] = 5.0 * points[:, 2]

    grid.point_data["Mach"] = mach
    grid.point_data["Pressure"] = pressure
    grid.point_data["Temperature"] = temperature
    grid.point_data["Velocity"] = velocity
    grid.point_data["Density"] = pressure / (287.058 * temperature)
    return grid.cast_to_unstructured_grid()


@pytest.fixture(scope="module")
def surface_solution(flow_solution) -> pv.DataSet:
    """A solution that also carries a pressure coefficient."""
    dataset = flow_solution.copy()
    pressure = dataset.point_data["Pressure"]
    dataset.point_data["Pressure_Coefficient"] = (
        pressure - 101325.0
    ) / (0.5 * 1.225 * 200.0**2)
    return dataset


def image_is_not_blank(path: Path, minimum_colours: int = 50) -> bool:
    """True when a PNG contains real content rather than a flat background."""
    from PIL import Image  # imported lazily; Pillow ships with pyvista

    with Image.open(path) as handle:
        array = np.asarray(handle.convert("RGB"))
    unique = np.unique(array.reshape(-1, 3), axis=0)
    return unique.shape[0] >= minimum_colours


# ---------------------------------------------------------------------------
# Field resolution
# ---------------------------------------------------------------------------


def test_resolve_field_finds_canonical_names(flow_solution):
    """Physical quantities map onto whatever the solver called them."""
    assert resolve_field(flow_solution, "mach") == "Mach"
    assert resolve_field(flow_solution, "pressure") == "Pressure"
    assert resolve_field(flow_solution, "temperature") == "Temperature"
    assert resolve_field(flow_solution, "velocity") == "Velocity"


def test_resolve_field_is_case_insensitive():
    """SU2 spellings vary between releases, so matching tolerates case."""
    grid = pv.ImageData(dimensions=(3, 3, 3)).cast_to_unstructured_grid()
    grid.point_data["MACH"] = np.ones(grid.n_points)
    assert resolve_field(grid, "mach") == "MACH"


def test_missing_field_error_lists_what_is_present(flow_solution):
    """The error must help, not just say 'KeyError'."""
    with pytest.raises(VisualizationError) as excinfo:
        resolve_field(flow_solution, "heat_flux")
    message = str(excinfo.value)
    assert "heat_flux" in message
    assert "Mach" in message, "the error should list the available arrays"


def test_available_fields_lists_arrays(flow_solution):
    """Every point array is reported."""
    fields = available_fields(flow_solution)
    assert "Mach" in fields and "Pressure" in fields


def test_scalar_bar_titles_carry_units():
    """Scalar bars name the quantity and its units."""
    assert scalar_bar_title("pressure") == "Pressure [Pa]"
    assert scalar_bar_title("mach") == "Mach [-]"
    assert scalar_bar_title("temperature") == "Temperature [K]"
    assert scalar_bar_title("pressure", "Custom") == "Custom"
    assert field_units("velocity") == "m/s"


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


def test_resolution_presets_include_4k():
    """4K is the documented default for report figures."""
    assert RESOLUTIONS["4k"] == (3840, 2160)
    assert RenderSettings(resolution="4k").window_size() == (3840, 2160)


def test_unknown_resolution_is_rejected():
    """A typo names the valid options rather than rendering something odd."""
    with pytest.raises(VisualizationError, match="unknown resolution"):
        RenderSettings(resolution="8k").window_size()


def test_required_colormaps_are_available():
    """The Fluent-style palettes named in the specification are present."""
    for name in ("turbo", "coolwarm", "viridis"):
        assert name in COLORMAPS
        assert RenderSettings(colormap=name).validated_colormap() == name


def test_unknown_colormap_is_rejected():
    """An unsupported palette is caught before rendering."""
    with pytest.raises(VisualizationError, match="unknown colormap"):
        RenderSettings(colormap="rainbow_unicorn").validated_colormap()


def test_unknown_camera_view_is_rejected(flow_solution, tmp_path):
    """Camera views are validated against the named set."""
    settings = RenderSettings(resolution="preview", camera_view="sideways")
    with pytest.raises(VisualizationError, match="unknown camera view"):
        render_surface_pressure(flow_solution, tmp_path / "x.png", settings)


# ---------------------------------------------------------------------------
# The four visualisation modes
# ---------------------------------------------------------------------------


def test_surface_pressure_renders(surface_solution, tmp_path):
    """Mode 1: surface pressure map, preferring the pressure coefficient."""
    path = render_surface_pressure(
        surface_solution,
        tmp_path / "cp.png",
        RenderSettings(resolution="preview", colormap="turbo"),
    )
    assert path.is_file()
    assert image_is_not_blank(path)


def test_surface_pressure_falls_back_to_absolute_pressure(flow_solution, tmp_path):
    """Without a C_p array the absolute pressure is shown instead."""
    path = render_surface_pressure(
        flow_solution,
        tmp_path / "p.png",
        RenderSettings(resolution="preview"),
        use_coefficient=True,
    )
    assert image_is_not_blank(path)


def test_mach_slice_renders(flow_solution, tmp_path):
    """Mode 2: Mach slice through the shock system."""
    path = render_mach_slice(
        flow_solution,
        tmp_path / "mach.png",
        RenderSettings(resolution="preview", camera_view="side"),
        slice_normal=(0.0, 1.0, 0.0),
    )
    assert image_is_not_blank(path)


def test_mach_slice_with_contours(flow_solution, tmp_path):
    """Iso-contours make individual shock angles measurable."""
    path = render_mach_slice(
        flow_solution,
        tmp_path / "mach_contour.png",
        RenderSettings(resolution="preview"),
        contour_levels=10,
    )
    assert image_is_not_blank(path)


def test_mach_slice_rejects_a_degenerate_normal(flow_solution, tmp_path):
    """A zero normal defines no plane."""
    with pytest.raises(VisualizationError, match="non-zero vector"):
        render_mach_slice(
            flow_solution,
            tmp_path / "bad.png",
            RenderSettings(resolution="preview"),
            slice_normal=(0.0, 0.0, 0.0),
        )


def test_mach_slice_rejects_a_plane_outside_the_domain(flow_solution, tmp_path):
    """A plane that misses the data is an error, not an empty picture."""
    with pytest.raises(VisualizationError, match="does not intersect"):
        render_mach_slice(
            flow_solution,
            tmp_path / "bad.png",
            RenderSettings(resolution="preview"),
            slice_normal=(0.0, 1.0, 0.0),
            slice_origin=(0.0, 50.0, 0.0),
        )


def test_streamlines_render(flow_solution, tmp_path):
    """Mode 3: streamlines seeded at the intake, coloured by velocity."""
    path = render_streamlines(
        flow_solution,
        tmp_path / "streams.png",
        RenderSettings(resolution="preview"),
        seed_center=(-0.1, 0.0, 0.0),
        seed_radius=0.05,
        n_points=60,
    )
    assert image_is_not_blank(path)


def test_streamlines_can_be_coloured_by_temperature(flow_solution, tmp_path):
    """Colouring by temperature shows air warmed before it reaches the die."""
    path = render_streamlines(
        flow_solution,
        tmp_path / "streams_t.png",
        RenderSettings(resolution="preview", colormap="coolwarm"),
        seed_center=(-0.1, 0.0, 0.0),
        seed_radius=0.05,
        n_points=60,
        colour_by="temperature",
    )
    assert image_is_not_blank(path)


def test_thermal_slice_renders_with_a_sensor_marker(flow_solution, tmp_path):
    """Mode 4: temperature slice with the BMP580 location marked."""
    path = render_thermal(
        flow_solution,
        tmp_path / "thermal.png",
        RenderSettings(resolution="preview", colormap="coolwarm"),
        probe_point=(0.2, 0.0, 0.0),
    )
    assert image_is_not_blank(path)


def test_thermal_surface_mode_renders(flow_solution, tmp_path):
    """Passing no slice normal renders the surface temperature instead."""
    path = render_thermal(
        flow_solution,
        tmp_path / "thermal_surface.png",
        RenderSettings(resolution="preview", colormap="coolwarm"),
        slice_normal=None,
    )
    assert image_is_not_blank(path)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,kwargs",
    [
        ("surface_pressure", {}),
        ("mach_slice", {}),
        ("streamlines", {"seed_radius": 0.05, "n_points": 40}),
        ("thermal", {}),
    ],
)
def test_dispatcher_renders_every_mode(flow_solution, tmp_path, mode, kwargs):
    """The MCP entry point covers all four documented modes."""
    path = render_visualization(
        flow_solution,
        mode,
        tmp_path / f"{mode}.png",
        resolution="preview",
        colormap="viridis" if mode != "thermal" else "coolwarm",
        **kwargs,
    )
    assert Path(path).is_file()
    assert image_is_not_blank(Path(path))


def test_dispatcher_rejects_an_unknown_mode(flow_solution, tmp_path):
    """An unknown mode names the valid ones."""
    with pytest.raises(VisualizationError, match="unknown visualization type"):
        render_visualization(flow_solution, "hologram", tmp_path / "x.png")


def test_dispatcher_accepts_every_named_camera_view(flow_solution, tmp_path):
    """All named viewpoints produce a render."""
    for view in CAMERA_VIEWS:
        path = render_visualization(
            flow_solution,
            "surface_pressure",
            tmp_path / f"{view}.png",
            camera_view=view,
            resolution="preview",
        )
        assert Path(path).is_file()


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


def test_probe_reads_a_field_at_a_point(flow_solution):
    """Probing is how the BMP580 die temperature is read out."""
    value = probe_point_value(flow_solution, (0.2, 0.0, 0.0), "temperature")
    assert 280.0 < value < 400.0


def test_probe_outside_the_domain_is_an_error(flow_solution):
    """Silently returning zero for an outside point would be dangerous.

    The sensor temperature is the headline number of the thermal track; a
    mistyped coordinate must fail loudly rather than report 0 K.
    """
    with pytest.raises(VisualizationError, match="outside the solution domain"):
        probe_point_value(flow_solution, (100.0, 100.0, 100.0), "temperature")


# ---------------------------------------------------------------------------
# Loading and export
# ---------------------------------------------------------------------------


def test_load_solution_round_trips(flow_solution, tmp_path):
    """A written solution reads back with its fields intact."""
    path = tmp_path / "flow.vtu"
    flow_solution.save(str(path))
    loaded = load_solution(path)
    assert loaded.n_points == flow_solution.n_points
    assert "Mach" in available_fields(loaded)


def test_load_missing_file_is_reported(tmp_path):
    """A bad path is reported clearly."""
    with pytest.raises(VisualizationError, match="not found"):
        load_solution(tmp_path / "absent.vtu")


def test_find_solution_file_prefers_the_flow_output(flow_solution, tmp_path):
    """SU2's volume output is located inside a run directory."""
    flow_solution.save(str(tmp_path / "flow.vtu"))
    flow_solution.save(str(tmp_path / "other.vtu"))
    assert find_solution_file(tmp_path).name == "flow.vtu"


def test_find_solution_file_reports_an_empty_directory(tmp_path):
    """A run directory with no solution says so."""
    with pytest.raises(VisualizationError, match="no volume solution"):
        find_solution_file(tmp_path)


def test_dispatcher_accepts_a_run_directory(flow_solution, tmp_path):
    """A run directory is resolved to its solution file."""
    run = tmp_path / "run"
    run.mkdir()
    flow_solution.save(str(run / "flow.vtu"))
    path = render_visualization(
        run, "mach_slice", tmp_path / "from_dir.png", resolution="preview"
    )
    assert Path(path).is_file()


def test_export_vtk(flow_solution, tmp_path):
    """VTK export feeds downstream analysis tools."""
    path = export_scene(flow_solution, tmp_path / "scene.vtu")
    assert path.is_file() and path.stat().st_size > 0


def test_export_gltf(flow_solution, tmp_path):
    """glTF export gives a 3D scene that opens in a browser."""
    path = export_scene(flow_solution, tmp_path / "scene.gltf")
    assert path.is_file() and path.stat().st_size > 0


def test_export_rejects_an_unknown_format(flow_solution, tmp_path):
    """An unsupported extension names the supported ones."""
    with pytest.raises(VisualizationError, match="unsupported export format"):
        export_scene(flow_solution, tmp_path / "scene.dwg")


def test_rendered_image_has_the_requested_resolution(flow_solution, tmp_path):
    """The written PNG matches the requested pixel dimensions."""
    from PIL import Image

    path = render_visualization(
        flow_solution,
        "surface_pressure",
        tmp_path / "hd.png",
        resolution="hd",
    )
    with Image.open(path) as handle:
        assert handle.size == RESOLUTIONS["hd"]


# ---------------------------------------------------------------------------
# A body inside a farfield, and the rocket frame
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rocket_solution() -> pv.DataSet:
    """A flow volume with a body-shaped hole, laid out as the solver has it.

    The body runs from the origin along +X, the domain extends far beyond it
    in every direction, and the outer boundary is therefore two separate
    pieces -- the body and the farfield -- as in a real SU2 solution.
    """
    grid = pv.ImageData(dimensions=(41, 21, 21), spacing=(0.1, 0.1, 0.1))
    grid.origin = (-1.0, -1.0, -1.0)
    centres = grid.cell_centers().points
    radius = np.linalg.norm(centres[:, 1:], axis=1)
    inside = (centres[:, 0] > 0.0) & (centres[:, 0] < 1.0) & (radius < 0.15)
    volume = grid.extract_cells(np.flatnonzero(~inside))

    points = volume.points
    x = points[:, 0]
    volume.point_data["Mach"] = 1.3 - 0.4 * np.exp(-8.0 * (x**2)) + 0.05 * x
    volume.point_data["Pressure"] = 101325.0 * (1.0 + 0.3 * np.exp(-5.0 * x**2))
    velocity = np.zeros_like(points)
    velocity[:, 0] = 440.0
    volume.point_data["Velocity"] = velocity
    return volume


def test_the_body_is_separated_from_the_farfield(rocket_solution):
    """A surface render of the farfield would hide the rocket inside it."""
    from backend.visualizer import _extract_walls

    walls = _extract_walls(rocket_solution)
    assert walls is not None and walls.n_points
    low, high = np.array(walls.bounds[::2]), np.array(walls.bounds[1::2])
    # The hole, not the four-metre domain around it.
    assert high[0] - low[0] == pytest.approx(1.0, abs=0.11)
    assert high[1] - low[1] < 0.5


def test_a_domain_with_no_body_has_no_walls(flow_solution):
    """The outer skin of an empty box is not a body to draw over a slice."""
    from backend.visualizer import _extract_walls

    assert _extract_walls(flow_solution) is None


def test_the_rocket_frame_stands_the_body_nose_up(rocket_solution):
    """Solver +X (nose to tail) becomes rocket -Z; velocity turns with it."""
    from backend.visualizer import _extract_walls, to_rocket_frame

    rocket = to_rocket_frame(rocket_solution)
    walls = _extract_walls(rocket)
    low, high = np.array(walls.bounds[::2]), np.array(walls.bounds[1::2])
    # Nose at the origin, body below it along -Z.
    assert high[2] == pytest.approx(0.0, abs=0.11)
    assert low[2] == pytest.approx(-1.0, abs=0.11)
    # The air now moves downwards, past a rocket climbing nose-first.
    velocity = np.asarray(rocket.point_data["Velocity"])
    assert np.allclose(velocity[:, 2], -440.0)
    assert np.allclose(velocity[:, 0], 0.0, atol=1e-9)
    # The source is untouched.
    assert rocket_solution.bounds[1] == pytest.approx(3.0)


def test_a_mach_slice_is_cropped_to_the_body(rocket_solution, tmp_path, monkeypatch):
    """The shock system must fill the picture, not a speck in the farfield."""
    import backend.visualizer as visualizer

    captured = {}
    original = visualizer.apply_camera

    def spy(plotter, dataset, settings, face_normal=None):
        captured["bounds"] = dataset.bounds
        return original(plotter, dataset, settings)

    monkeypatch.setattr(visualizer, "apply_camera", spy)
    render_mach_slice(
        rocket_solution,
        tmp_path / "cropped.png",
        RenderSettings(resolution="preview"),
    )
    low, high = np.array(captured["bounds"][::2]), np.array(captured["bounds"][1::2])
    # One body length plus three quarters of one either side, not four metres.
    assert high[0] - low[0] <= 2.6
    assert high[0] - low[0] < rocket_solution.bounds[1] - rocket_solution.bounds[0]

    render_mach_slice(
        rocket_solution,
        tmp_path / "whole.png",
        RenderSettings(resolution="preview"),
        crop_margin=None,
    )
    assert captured["bounds"][1] - captured["bounds"][0] == pytest.approx(4.0)


@pytest.mark.parametrize("view", sorted(CAMERA_VIEWS))
def test_every_view_renders_in_the_rocket_frame(rocket_solution, tmp_path, view):
    """The same view names work with the rocket standing up."""
    path = render_visualization(
        rocket_solution,
        "mach_slice",
        tmp_path / f"{view}.png",
        camera_view=view,
        resolution="preview",
        frame="rocket",
    )
    assert image_is_not_blank(path)


def test_the_surface_render_shows_the_body(rocket_solution, tmp_path):
    path = render_visualization(
        rocket_solution,
        "surface_pressure",
        tmp_path / "surface.png",
        resolution="preview",
        frame="rocket",
    )
    assert image_is_not_blank(path, minimum_colours=20)


def test_an_unknown_frame_is_rejected(rocket_solution, tmp_path):
    with pytest.raises(VisualizationError, match="frame"):
        render_visualization(
            rocket_solution, "mach_slice", tmp_path / "x.png", frame="body"
        )


def test_a_second_copy_of_the_farfield_is_not_taken_for_the_body(rocket_solution):
    """SU2's multiblock output carries the farfield twice.

    Once as the volume's outer skin, once as its own boundary block. Dropping
    only the largest piece left the other one to be drawn translucent over
    every slice, and stretched the crop box to the whole domain -- the
    washed-out, uncropped pictures an operator sent back.
    """
    from backend.visualizer import _extract_walls

    skin = rocket_solution.extract_surface()
    outer = skin.connectivity("largest").extract_surface()
    combined = pv.merge([rocket_solution, outer.cast_to_unstructured_grid()],
                        merge_points=False)
    walls = _extract_walls(combined)
    low, high = np.array(walls.bounds[::2]), np.array(walls.bounds[1::2])
    assert high[0] - low[0] == pytest.approx(1.0, abs=0.11)


def test_the_mach_range_follows_the_picture_not_its_extremes():
    """A stagnation zero must not squeeze a shock into one shade."""
    from backend.visualizer import _area_weighted_range

    plane = pv.Plane(i_resolution=100, j_resolution=100).triangulate()
    mach = 1.1 + 0.2 * (plane.points[:, 0] + 0.5)  # 1.1 to 1.3 across
    mach[:5] = 0.0  # a stagnation point and a sliver of wake
    plane.point_data["Mach"] = mach
    low, high = _area_weighted_range(plane, "Mach")
    assert high == pytest.approx(1.3, abs=0.01)
    # Not 0 (the extreme), and deep enough to show the flow behind a shock.
    assert 0.0 < low <= 0.6 * 1.3 + 1e-9


def test_the_camera_frames_the_whole_model(rocket_solution):
    """A tall rocket in a wide image lost its nose and tail to the old fit."""
    from backend.visualizer import apply_camera, to_rocket_frame

    rocket = to_rocket_frame(rocket_solution)
    for view in ("side", "isometric", "front"):
        plotter = pv.Plotter(off_screen=True, window_size=(1920, 1080))
        settings = RenderSettings(resolution="hd", camera_view=view, frame="rocket")
        apply_camera(plotter, rocket, settings)
        camera = plotter.camera
        assert camera.parallel_projection
        low, high = np.array(rocket.bounds[::2]), np.array(rocket.bounds[1::2])
        # Parallel scale is half the visible height; the model must fit in it.
        if view == "side":
            assert camera.parallel_scale >= 0.5 * (high[2] - low[2])
        plotter.close()


def test_body_streamlines_pass_the_body(rocket_solution, tmp_path):
    """Seeded ahead of the nose, not in open air beside it."""
    path = render_visualization(
        rocket_solution, "streamlines", tmp_path / "s.png",
        camera_view="side", resolution="preview", frame="rocket",
    )
    assert image_is_not_blank(path, minimum_colours=20)


def test_the_surface_colour_range_ignores_the_stagnation_point(rocket_solution, tmp_path, monkeypatch):
    import backend.visualizer as visualizer

    seen = {}
    original = visualizer.pv.Plotter.add_mesh

    def spy(self, mesh, *args, **kwargs):
        if kwargs.get("scalars"):
            seen["clim"] = kwargs.get("clim")
        return original(self, mesh, *args, **kwargs)

    monkeypatch.setattr(visualizer.pv.Plotter, "add_mesh", spy)
    render_visualization(
        rocket_solution, "surface_pressure", tmp_path / "p.png",
        resolution="preview", frame="rocket",
    )
    pressure = np.asarray(rocket_solution.point_data["Pressure"])
    assert seen["clim"] is not None
    assert seen["clim"][1] <= pressure.max()


def test_every_image_names_its_speed_and_angle(tmp_path):
    """The caption comes from the run's own solver config, sweep points too."""
    from backend.visualizer import flow_caption, flow_condition

    point = tmp_path / "point_003"
    point.mkdir()
    (point / "solver.cfg").write_text(
        "MACH_NUMBER= 0.500000\nAOA= 7.000000\nSIDESLIP_ANGLE= 0.000000\n"
        "FREESTREAM_TEMPERATURE= 288.150000\n"
    )
    condition = flow_condition(point)
    assert condition["mach"] == 0.5 and condition["aoa_deg"] == 7.0
    assert abs(condition["speed_ms"] - 170.1) < 0.5
    caption = flow_caption(point)
    assert "Mach 0.50" in caption and "170 m/s" in caption and "AoA 7°" in caption
    assert flow_caption(tmp_path / "nothing") == ""


def test_a_slice_is_seen_face_on_from_any_named_view():
    """From an angle a slice was a skewed parallelogram with the field squashed."""
    import pyvista as pv

    from backend.visualizer import RenderSettings, apply_camera

    plane = pv.Plane(center=(0, 0, 0), direction=(0, 1, 0), i_size=1.0, j_size=2.0)
    plotter = pv.Plotter(off_screen=True, window_size=(320, 180))
    try:
        apply_camera(
            plotter, plane, RenderSettings(resolution="hd", camera_view="isometric", frame="rocket"),
            face_normal=(0.0, 1.0, 0.0),
        )
        position, focal, _up = plotter.camera_position
        view = np.subtract(position, focal)
        view /= np.linalg.norm(view)
        assert abs(abs(view[1]) - 1.0) < 1e-9
    finally:
        plotter.close()
