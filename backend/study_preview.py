"""Pictures of the sensor-study geometries, so nobody has to imagine them.

A radiation shield or a housing with a plenum, a baffle and a fan is hard to
picture from a list of dimensions. These renders show the actual geometry a
study meshes -- the built-in one, or the operator's STEP turned and scaled
exactly as the CFD case does it -- with what acts on it drawn over the top:

* **Radiation shield**: the louvre stack cut open along the wind, the
  thermometer point, the sun from above, the long-wave flux (or the hot
  roof) from below and the wind; beside it the shield in its fluid domain.
* **SPS30 housing**: the outside with the oncoming air and the side slits;
  beside it the housing cut open lengthwise, showing the plenum, the baffle
  the air has to turn over, the sensor chamber, the SPS30 intake with its
  fan suction, and the weep hole.

Both are drawn off screen with PyVista and saved as one PNG.
"""

from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

import numpy as np

os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")

PREVIEW_SIZE = (1500, 700)
# Bumped whenever the drawing changes, so cached pictures are redrawn.
PREVIEW_VERSION = 2
BACKGROUND = ("#1b2130", "#0e1118")
SOLID = "#c9d3e2"
SECTION = "#8aa0c0"
WIND = "#4fc3f7"
SUN = "#ffd54f"
BOTTOM = "#ff8a65"
SENSOR = "#66bb6a"
FAN = "#ab47bc"
PROBE = "#ef5350"
LABEL = "white"


class PreviewError(RuntimeError):
    """The geometry could not be drawn."""


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


def _tessellate(step: Path, scale: float, angle_z: float, shift: np.ndarray | None,
                size: float | None = None):
    """Triangulate a STEP file the way the CFD case places it."""
    import gmsh
    import pyvista as pv

    from backend.gmsh_session import gmsh_session

    with gmsh_session("study_preview"):
        occ = gmsh.model.occ
        solids = [e for e in occ.importShapes(str(step)) if e[0] == 3]
        if not solids:
            raise PreviewError(f"{step.name} contains no solid")
        if scale != 1.0:
            occ.dilate(solids, 0.0, 0.0, 0.0, scale, scale, scale)
        if abs(angle_z) > 1e-12:
            occ.rotate(solids, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, angle_z)
        occ.synchronize()
        low = np.full(3, np.inf)
        high = np.full(3, -np.inf)
        for dim, tag in solids:
            box = gmsh.model.getBoundingBox(dim, tag)
            low, high = np.minimum(low, box[:3]), np.maximum(high, box[3:])
        if shift is not None:
            occ.translate(solids, *(shift - 0.5 * (low + high)))
            occ.synchronize()
        if size is None:
            # From the model itself, not from a size typed into a form: a
            # 5 cm shield drawn with 5 mm triangles loses its plates.
            size = float(max(high - low)) / 60.0
        gmsh.option.setNumber("Mesh.MeshSizeMax", size)
        gmsh.option.setNumber("Mesh.MeshSizeMin", 0.1 * size)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 12)
        gmsh.model.mesh.generate(2)
        tags, coords, _ = gmsh.model.mesh.getNodes()
        index = {int(t): i for i, t in enumerate(tags)}
        points = np.asarray(coords).reshape(-1, 3)
        triangles: list[np.ndarray] = []
        types, _, nodes = gmsh.model.mesh.getElements(2)
        for kind, node_list in zip(types, nodes):
            if kind == 2:  # 3-node triangles
                triangles.append(np.asarray(node_list, dtype=np.int64).reshape(-1, 3))
    if not triangles:
        raise PreviewError(f"could not triangulate {step.name}")
    tri = np.vectorize(index.get)(np.vstack(triangles))
    faces = np.hstack([np.full((len(tri), 1), 3, dtype=np.int64), tri]).ravel()
    return pv.PolyData(points, faces).clean()


def _inside(surface, point) -> bool:
    """Is a point inside the solid this closed surface bounds?"""
    from backend.shield_cfd import point_inside_shield

    triangles = surface.triangulate().faces.reshape(-1, 4)[:, 1:]
    return point_inside_shield(np.asarray(surface.points)[triangles], point)


def _arrow(plotter, start, direction, length, colour, shaft=0.04, tip=0.12):
    import pyvista as pv

    direction = np.asarray(direction, dtype=float)
    direction = direction / np.linalg.norm(direction)
    plotter.add_mesh(
        pv.Arrow(start=start, direction=direction, scale=length,
                 shaft_radius=shaft, tip_radius=tip, tip_length=0.3),
        color=colour,
    )


def _labels(plotter, points, texts, colour=LABEL, size=14):
    plotter.add_point_labels(
        np.asarray(points, dtype=float), list(texts), font_size=size,
        text_color=colour, shape_color="#0e1118", shape_opacity=0.55,
        point_size=1, show_points=False, always_visible=True,
    )


def _finish(plotter, view, up, zoom=1.0):
    plotter.view_vector(view, up)
    plotter.reset_camera()
    plotter.camera.zoom(zoom)
    plotter.add_axes(color="white")


def _destination(output_path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Radiation shield
# ---------------------------------------------------------------------------


def render_shield_preview(setup, output_path: Path | str, caption: str = "") -> Path:
    """The shield cut open with its loads, and the shield in its domain."""
    import pyvista as pv

    from backend.shield_cfd import resolve_shield_step
    from core.shield_models import BottomMode

    pv.OFF_SCREEN = True
    output_path = _destination(output_path)
    with tempfile.TemporaryDirectory() as scratch:
        step, scale = resolve_shield_step(setup, Path(scratch))
        domain = np.asarray(setup.domain_size_m, dtype=float)
        centre = np.array([0.0, 0.0, 0.5 * domain[2]])
        shield = _tessellate(step, scale, 0.0, centre)

    low = np.array(shield.bounds[0::2])
    high = np.array(shield.bounds[1::2])
    extent = high - low
    span = float(max(extent))
    probe = centre + np.asarray(setup.thermometer_xyz_m, dtype=float)
    probe_inside = _inside(shield, probe)
    roof = setup.bottom_mode is BottomMode.ROOF_TEMPERATURE
    bottom_text = (
        f"hot roof {setup.bottom_temperature_k:.0f} K below"
        if roof
        else f"ground long-wave {setup.bottom_flux_w_m2:.0f} W/m2"
    )

    plotter = pv.Plotter(off_screen=True, shape=(1, 2), window_size=PREVIEW_SIZE)
    try:
        plotter.set_background(BACKGROUND[0], top=BACKGROUND[1])

        # Left: the stack cut along the wind through the thermometer.
        plotter.subplot(0, 0)
        cut = shield.clip(normal=(0.0, 1.0, 0.0), origin=probe, invert=False)
        plotter.add_mesh(cut, color=SECTION, smooth_shading=False, show_edges=False)
        plotter.add_mesh(pv.Sphere(radius=0.035 * span, center=probe), color=PROBE)
        for dx in (-0.3, 0.0, 0.3):
            _arrow(plotter, [probe[0] + dx * span, probe[1], high[2] + 0.75 * span],
                   (0, 0, -1), 0.55 * span, SUN)
            _arrow(plotter, [probe[0] + dx * span, probe[1], low[2] - 0.75 * span],
                   (0, 0, 1), 0.55 * span, BOTTOM)
        for dz in (0.25, 0.5, 0.75):
            _arrow(plotter, [low[0] - 0.9 * span, probe[1], low[2] + dz * extent[2]],
                   (1, 0, 0), 0.6 * span, WIND)
        _labels(
            plotter,
            [
                [probe[0], probe[1], high[2] + 0.85 * span],
                [probe[0], probe[1], low[2] - 0.85 * span],
                [low[0] - 0.6 * span, probe[1], high[2] + 0.1 * span],
                probe + np.array([0.0, 0.0, 0.12 * span]),
            ],
            [
                f"sun {setup.solar_flux_w_m2:.0f} W/m2",
                bottom_text,
                f"wind {setup.wind_speed_ms:g} m/s",
                "thermometer (monitor point)",
            ],
        )
        plotter.add_text("Shield cut open along the wind", position="upper_left",
                         font_size=11, color=LABEL)
        if probe_inside:
            plotter.add_text(
                "WARNING: the thermometer point is inside the shield material -\n"
                "move it into the air (Thermometer offset)",
                position="lower_left", font_size=10, color=PROBE,
            )
        _finish(plotter, (0.25, -1.0, 0.2), (0, 0, 1), 1.05)

        # Right: the whole shield in its fluid domain.
        plotter.subplot(0, 1)
        box = pv.Box(bounds=(-0.5 * domain[0], 0.5 * domain[0], -0.5 * domain[1],
                             0.5 * domain[1], 0.0, domain[2]))
        plotter.add_mesh(box, style="wireframe", color="#7f8ea3", line_width=1.5)
        for z, colour, opacity in ((0.0, BOTTOM, 0.3), (domain[2], SUN, 0.18)):
            plotter.add_mesh(
                pv.Plane(center=(0.0, 0.0, z), direction=(0, 0, 1),
                         i_size=domain[0], j_size=domain[1]),
                color=colour, opacity=opacity,
            )
        plotter.add_mesh(shield, color=SOLID, smooth_shading=True)
        _arrow(plotter, [-0.5 * domain[0] - 0.35 * domain[0], 0.0, centre[2]],
               (1, 0, 0), 0.3 * domain[0], WIND, shaft=0.05, tip=0.14)
        _labels(
            plotter,
            [
                [-0.5 * domain[0], 0.0, centre[2] + 0.1 * domain[2]],
                [0.5 * domain[0], 0.0, centre[2]],
                [0.0, 0.0, domain[2]],
                [0.0, 0.0, 0.0],
                [0.0, 0.0, centre[2] + 0.6 * span],
            ],
            [
                "inlet",
                "outlet",
                "top: solar",
                "bottom: " + ("roof" if roof else "ground"),
                f"shield {extent[0] * 100:.0f} x {extent[1] * 100:.0f} x {extent[2] * 100:.0f} cm",
            ],
        )
        plotter.add_text(
            f"Domain {domain[0]:g} x {domain[1]:g} x {domain[2]:g} m",
            position="upper_left", font_size=11, color=LABEL,
        )
        if caption:
            plotter.add_text(caption, position="lower_left", font_size=9, color=LABEL)
        _finish(plotter, (0.9, -1.0, 0.55), (0, 0, 1), 1.0)
        plotter.screenshot(str(output_path))
    finally:
        plotter.close()
    return output_path


# ---------------------------------------------------------------------------
# SPS30 housing
# ---------------------------------------------------------------------------


def render_sps30_preview(setup, output_path: Path | str, caption: str = "") -> Path:
    """The housing from outside with the air, and cut open with its parts."""
    import pyvista as pv

    from backend.sps30_cfd import _normal_vector, resolve_housing_step, travel_rotation

    pv.OFF_SCREEN = True
    output_path = _destination(output_path)
    rotation = travel_rotation(setup.forward_axis)
    angle = math.atan2(rotation[1, 0], rotation[0, 0])
    with tempfile.TemporaryDirectory() as scratch:
        step, scale = resolve_housing_step(setup, Path(scratch))
        housing = _tessellate(step, scale, angle, None)

    low = np.array(housing.bounds[0::2])
    high = np.array(housing.bounds[1::2])
    extent = high - low
    span = float(max(extent))
    mid = 0.5 * (low + high)
    sensor = rotation @ np.asarray(setup.sensor_face_center_m, dtype=float)
    normal = rotation @ _normal_vector(setup.sensor_face_normal)
    built_in = not setup.housing_step_path

    plotter = pv.Plotter(off_screen=True, shape=(1, 2), window_size=PREVIEW_SIZE)
    try:
        plotter.set_background(BACKGROUND[0], top=BACKGROUND[1])

        # Left: outside, with the oncoming air and the yaw.
        plotter.subplot(0, 0)
        plotter.add_mesh(housing, color=SOLID, smooth_shading=False)
        yaw = math.radians(setup.yaw_deg)
        wind = np.array([math.cos(yaw), math.sin(yaw), 0.0])
        for dz in (0.3, 0.6, 0.9):
            start = np.array([low[0] - 0.8 * span, mid[1], low[2] + dz * extent[2]])
            _arrow(plotter, start - wind * 0.0, wind, 0.5 * span, WIND)
        labels = [[low[0] - 0.55 * span, mid[1], high[2] + 0.15 * span]]
        texts = [f"air {setup.speed_ms:g} m/s, yaw {setup.yaw_deg:g} deg"]
        _arrow(plotter, [high[0] + 0.1 * span, mid[1], high[2] + 0.25 * span],
               (-1, 0, 0), 0.35 * span, "#9ccc65")
        labels.append([high[0] + 0.25 * span, mid[1], high[2] + 0.4 * span])
        texts.append("travel")
        if built_in:
            slit = rotation @ np.array([0.025, -0.035, 0.0465])
            labels.append(slit + np.array([0.0, -0.01, 0.0]))
            texts.append("side slit (static port)")
        _labels(plotter, labels, texts)
        plotter.add_text("Housing from outside", position="upper_left",
                         font_size=11, color=LABEL)
        _finish(plotter, (-0.7, -1.0, 0.6), (0, 0, 1), 0.95)

        # Right: cut open lengthwise through the sensor.
        plotter.subplot(0, 1)
        cut = housing.clip(normal=(0.0, 1.0, 0.0), origin=[0.0, sensor[1], 0.0],
                           invert=False)
        plotter.add_mesh(cut, color=SECTION, smooth_shading=False)
        width, height = setup.sensor_face_size_m[:2]
        face = pv.Plane(center=sensor - 0.0005 * normal, direction=normal,
                        i_size=max(width, 1e-3), j_size=max(height, 1e-3))
        plotter.add_mesh(face, color=SENSOR, lighting=False)
        labels = [sensor + normal * 0.02 + np.array([0.0, 0.0, 0.6 * height + 0.006])]
        texts = ["SPS30 intake"]
        if setup.fan_enabled:
            _arrow(plotter, sensor + normal * 0.035, -normal, 0.03, FAN,
                   shaft=0.05, tip=0.15)
            labels.append(sensor + normal * 0.04 + np.array([0.0, 0.0, -0.012]))
            texts.append(f"fan suction {setup.fan_flow_lpm:g} L/min")
        if built_in:
            parts = {
                "plenum": [0.03, 0.0, 0.025],
                "baffle": [0.0595, 0.0, 0.035],
                "sensor chamber": [0.085, 0.0, 0.065],
                "weep hole": [0.045, 0.0, -0.006],
                "slit": [0.025, 0.0, 0.055],
            }
            for name, where in parts.items():
                labels.append(rotation @ np.array(where))
                texts.append(name)
            path = rotation @ np.array(
                [[0.025, 0, 0.047], [0.045, 0, 0.04], [0.058, 0, 0.07],
                 [0.064, 0, 0.07], [0.08, 0, 0.045], [0.104, 0, 0.03]]
            ).T
            plotter.add_mesh(pv.Spline(path.T, 60).tube(radius=0.0012), color=WIND)
        _labels(plotter, labels, texts)
        plotter.add_text("Cut open: the air's way to the sensor", position="upper_left",
                         font_size=11, color=LABEL)
        if caption:
            plotter.add_text(caption, position="lower_left", font_size=9, color=LABEL)
        _finish(plotter, (0.0, -1.0, 0.15), (0, 0, 1), 1.1)
        plotter.screenshot(str(output_path))
    finally:
        plotter.close()
    return output_path


# ---------------------------------------------------------------------------
# Cached previews for the tools
# ---------------------------------------------------------------------------


def preview_key(kind: str, setup) -> str:
    """Identifies what a preview shows: every setup field and the STEP's age."""
    import hashlib
    import json

    data = setup.model_dump(mode="json")
    step = data.get("shield_step_path") or data.get("housing_step_path")
    if step and Path(step).is_file():
        data["_step_mtime"] = Path(step).stat().st_mtime
    data["_kind"] = kind
    data["_version"] = PREVIEW_VERSION
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]


def cached_preview(kind: str, setup, folder: Path | str | None = None) -> Path:
    """The preview for this setup, drawn once and reused while it is unchanged.

    ``kind`` is 'shield' or 'sps30'. The key covers every setup field (and a
    STEP file's modification time), so changing anything that is drawn
    draws it again.
    """
    if kind not in ("shield", "sps30"):
        raise PreviewError("kind must be 'shield' or 'sps30'")
    if folder is None:
        from core.platform_env import data_root

        folder = data_root() / "previews"
    key = preview_key(kind, setup)
    path = Path(folder) / f"{kind}-geometry-{key}.png"
    if path.is_file():
        return path
    render = render_shield_preview if kind == "shield" else render_sps30_preview
    return render(setup, path)
