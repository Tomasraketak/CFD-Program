"""Programmatic reference geometries used for demos and the test suite.

These let the mesh pipeline and solvers be exercised end to end without
shipping binary CAD fixtures, and give a new operator something to run on the
first launch. Both builders emit STEP files through OpenCASCADE, so they take
exactly the same import path as user-supplied CAD.
"""

from __future__ import annotations

import math
from pathlib import Path

import gmsh

from backend.gmsh_session import gmsh_session


def create_reference_rocket_step(
    output_path: Path | str,
    body_length: float = 1.0,
    body_diameter: float = 0.08,
    nose_length: float = 0.22,
    fin_count: int = 4,
    fin_root_chord: float = 0.12,
    fin_tip_chord: float = 0.06,
    fin_span: float = 0.07,
    fin_thickness: float = 0.004,
    nose_tip_radius: float = 0.0015,
    nose_axis: str = "+X",
) -> Path:
    """Write a STEP file of a finned sounding rocket.

    An ogive-approximating conical nose, a cylindrical airframe and a ring of
    tapered, knife-edged fins at the base. The body axis runs along
    ``nose_axis`` with the nose tip at the origin, which exercises the
    pipeline's orientation handling when a non-``+X`` axis is requested.

    Parameters
    ----------
    output_path:
        Destination ``.step`` path.
    body_length:
        Overall length from nose tip to base, metres.
    body_diameter:
        Airframe outer diameter, metres.
    nose_length:
        Length of the conical nose section, metres.
    fin_count:
        Number of equally spaced fins.
    fin_root_chord, fin_tip_chord, fin_span, fin_thickness:
        Fin planform and thickness, metres.
    nose_tip_radius:
        Spherical tip radius, metres. Zero gives a mathematically sharp apex,
        which Gmsh meshes with non-manifold edges; the default keeps the tip
        blunt enough to triangulate cleanly.
    nose_axis:
        One of ``+X``, ``+Y`` or ``+Z``. The body runs along this axis from
        the nose tip at the origin towards the tail, which is the same sense
        as the meshing parameter ``nose_direction``.

    Returns
    -------
    Path
        The written STEP file path.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if nose_length >= body_length:
        raise ValueError("nose_length must be shorter than body_length")
    if fin_root_chord > body_length - nose_length:
        raise ValueError("fin root chord does not fit on the cylindrical section")

    radius = 0.5 * body_diameter

    with gmsh_session("reference_rocket"):
        occ = gmsh.model.occ

        # Nose cone plus the cylindrical airframe. The tip carries a small
        # spherical cap rather than coming to a mathematical point: a true
        # apex is a meshing singularity that Gmsh triangulates with
        # non-manifold edges, which no boundary-layer march can consume. Real
        # nose cones have a finite tip radius for the same reason they have to
        # survive manufacture.
        tip_radius = max(nose_tip_radius, 0.0)
        if tip_radius > 0.0:
            # Offset the truncated cone so the finished tip still sits at x=0.
            half_angle = math.atan2(radius, nose_length)
            tip_centre_x = tip_radius / max(math.sin(half_angle), 1.0e-9)
            cone_base_radius = tip_radius / max(math.cos(half_angle), 1.0e-9)
            nose = occ.addCone(
                tip_centre_x,
                0.0,
                0.0,
                nose_length - tip_centre_x,
                0.0,
                0.0,
                cone_base_radius,
                radius,
            )
            cap = occ.addSphere(tip_centre_x, 0.0, 0.0, tip_radius)
            nose_parts, _ = occ.fuse([(3, nose)], [(3, cap)])
            nose = nose_parts[0][1]
        else:
            nose = occ.addCone(0.0, 0.0, 0.0, nose_length, 0.0, 0.0, 0.0, radius)

        barrel = occ.addCylinder(
            nose_length, 0.0, 0.0, body_length - nose_length, 0.0, 0.0, radius
        )

        solids = [(3, nose), (3, barrel)]

        # Fins: tapered plates swept from the root chord at the base.
        fin_x0 = body_length - fin_root_chord
        for index in range(fin_count):
            angle = 2.0 * math.pi * index / fin_count
            tag = _build_fin(
                occ,
                root_x=fin_x0,
                root_chord=fin_root_chord,
                tip_chord=fin_tip_chord,
                span=fin_span,
                thickness=fin_thickness,
                root_radius=radius * 0.95,
                angle=angle,
            )
            solids.append((3, tag))

        # Fuse everything into a single watertight solid.
        fused, _ = occ.fuse([solids[0]], solids[1:])
        occ.synchronize()

        if nose_axis != "+X":
            _rotate_to_axis(occ, fused, nose_axis)
            occ.synchronize()

        gmsh.write(str(output_path))

    return output_path


def _build_fin(
    occ,
    root_x: float,
    root_chord: float,
    tip_chord: float,
    span: float,
    thickness: float,
    root_radius: float,
    angle: float,
) -> int:
    """Build one tapered fin as a lofted solid and return its tag."""
    # Root profile: a thin rectangle in the x-z plane at the body radius.
    root_points = [
        occ.addPoint(root_x, -0.5 * thickness, root_radius),
        occ.addPoint(root_x + root_chord, -0.5 * thickness, root_radius),
        occ.addPoint(root_x + root_chord, 0.5 * thickness, root_radius),
        occ.addPoint(root_x, 0.5 * thickness, root_radius),
    ]
    # Tip profile: shorter chord, swept back, at the outer span station.
    tip_x = root_x + (root_chord - tip_chord)
    tip_radius = root_radius + span
    tip_points = [
        occ.addPoint(tip_x, -0.25 * thickness, tip_radius),
        occ.addPoint(tip_x + tip_chord, -0.25 * thickness, tip_radius),
        occ.addPoint(tip_x + tip_chord, 0.25 * thickness, tip_radius),
        occ.addPoint(tip_x, 0.25 * thickness, tip_radius),
    ]

    root_loop = _closed_loop(occ, root_points)
    tip_loop = _closed_loop(occ, tip_points)

    # Loft the two profiles into a solid fin.
    lofted = occ.addThruSections([root_loop, tip_loop], makeSolid=True, makeRuled=True)
    fin_tag = lofted[0][1]

    # Rotate the fin about the body axis to its azimuthal station.
    if angle != 0.0:
        occ.rotate([(3, fin_tag)], 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, angle)
    return fin_tag


def _closed_loop(occ, point_tags: list[int]) -> int:
    """Create a closed curve loop through the given points."""
    lines = [
        occ.addLine(point_tags[i], point_tags[(i + 1) % len(point_tags)])
        for i in range(len(point_tags))
    ]
    return occ.addCurveLoop(lines)


def _rotate_to_axis(occ, entities, nose_axis: str) -> None:
    """Rotate a +X-aligned model so its nose points along ``nose_axis``."""
    rotations = {
        "+Y": (0.0, 0.0, 1.0, math.pi / 2.0),
        "-Y": (0.0, 0.0, 1.0, -math.pi / 2.0),
        "+Z": (0.0, 1.0, 0.0, -math.pi / 2.0),
        "-Z": (0.0, 1.0, 0.0, math.pi / 2.0),
        "-X": (0.0, 0.0, 1.0, math.pi),
    }
    if nose_axis not in rotations:
        raise ValueError(f"unsupported nose_axis '{nose_axis}'")
    ax, ay, az, angle = rotations[nose_axis]
    occ.rotate(entities, 0.0, 0.0, 0.0, ax, ay, az, angle)


def create_sensor_enclosure_step(
    output_path: Path | str,
    housing_length: float = 0.060,
    housing_width: float = 0.040,
    housing_height: float = 0.030,
    wall_thickness: float = 0.002,
    tube_diameter: float = 0.008,
    tube_length: float = 0.030,
) -> Path:
    """Write a STEP file of the BMP580 enclosure with its sampling tube.

    A hollow rectangular housing with a forward-facing ram-air intake tube and
    a smaller rear vent, so air drawn in by vehicle motion passes over the
    sensor chamber and exits. The internal void is retained as the fluid
    region the conjugate solver meshes.

    Parameters
    ----------
    output_path:
        Destination ``.step`` path.
    housing_length, housing_width, housing_height:
        External dimensions of the printed housing, metres.
    wall_thickness:
        Printed wall thickness, metres.
    tube_diameter, tube_length:
        Sampling tube bore and protrusion length, metres.

    Returns
    -------
    Path
        The written STEP file path.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if 2.0 * wall_thickness >= min(housing_length, housing_width, housing_height):
        raise ValueError("wall thickness leaves no internal chamber")
    if tube_diameter >= min(housing_width, housing_height) - 2.0 * wall_thickness:
        raise ValueError("tube diameter does not fit the housing face")

    with gmsh_session("sensor_enclosure"):
        occ = gmsh.model.occ

        outer = occ.addBox(0.0, 0.0, 0.0, housing_length, housing_width, housing_height)
        inner = occ.addBox(
            wall_thickness,
            wall_thickness,
            wall_thickness,
            housing_length - 2.0 * wall_thickness,
            housing_width - 2.0 * wall_thickness,
            housing_height - 2.0 * wall_thickness,
        )
        shell, _ = occ.cut([(3, outer)], [(3, inner)])

        # Forward intake tube, protruding upstream along -X.
        centre_y = 0.5 * housing_width
        centre_z = 0.5 * housing_height
        tube_outer = occ.addCylinder(
            -tube_length,
            centre_y,
            centre_z,
            tube_length + wall_thickness,
            0.0,
            0.0,
            0.5 * tube_diameter + wall_thickness,
        )
        tube_bore = occ.addCylinder(
            -tube_length - wall_thickness,
            centre_y,
            centre_z,
            tube_length + 3.0 * wall_thickness,
            0.0,
            0.0,
            0.5 * tube_diameter,
        )

        # Rear vent, half the intake area so the chamber stays slightly pressurised.
        vent = occ.addCylinder(
            housing_length - 2.0 * wall_thickness,
            centre_y,
            centre_z,
            4.0 * wall_thickness,
            0.0,
            0.0,
            0.35 * tube_diameter,
        )

        fused, _ = occ.fuse(shell, [(3, tube_outer)])
        occ.cut(fused, [(3, tube_bore), (3, vent)])
        occ.synchronize()

        gmsh.write(str(output_path))

    return output_path
