"""Package an SPS30 housing study for ANSYS Fluent + Workbench/DesignXplorer.

Writes into a folder:

* ``fluid_domain.step`` -- the wind tunnel with the housing cut out, turned
  so the platform travels towards -x (wind along +x, z up), in metres;
* ``housing_placed.step`` -- the housing alone in the same position;
* ``design_points.csv`` -- the DoE, to copy into the Parameter Set and to
  fill in and import back into this program;
* ``README_FLUENT.md`` -- the settings for this study with its numbers.

The step-by-step procedure for beginners is ``docs/*/SPS30_STUDY_GUIDE.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from core.sps30_models import SPS30_LABELS, Sps30StudyParams


def export_fluent_package(params: Sps30StudyParams, folder: Path | str) -> Path:
    import gmsh

    from backend.gmsh_session import gmsh_session
    from backend.sps30_cfd import _normal_vector, _place
    from backend.sps30_study import air_properties, design_points, write_points_csv

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    setup = params.setup
    with gmsh_session("sps30_export"):
        occ = gmsh.model.occ
        solids, rotation = _place(occ, gmsh, setup, folder)
        low = np.full(3, np.inf)
        high = np.full(3, -np.inf)
        for dim, tag in solids:
            box = gmsh.model.getBoundingBox(dim, tag)
            low, high = np.minimum(low, box[:3]), np.maximum(high, box[3:])
        gmsh.write(str(folder / "housing_placed.step"))
        ref = float(max(high - low))
        m = setup.domain_multipliers
        d_low = low - np.array([m.get("upstream", 3.0), m.get("lateral", 3.0), m.get("vertical", 3.0)]) * ref
        d_high = high + np.array([m.get("downstream", 6.0), m.get("lateral", 3.0), m.get("vertical", 3.0)]) * ref
        box = occ.addBox(*d_low, *(d_high - d_low))
        occ.cut([(3, box)], solids)
        occ.synchronize()
        gmsh.write(str(folder / "fluid_domain.step"))
    built_in = folder / "sps30_housing.step"
    if built_in.exists() and not setup.housing_step_path:
        built_in.unlink()
    points = design_points(params)
    write_points_csv(folder / "design_points.csv", points)
    (folder / "study.json").write_text(json.dumps(params.model_dump(mode="json"), indent=2))

    sensor = rotation @ np.array(setup.sensor_face_center_m)
    normal = rotation @ _normal_vector(setup.sensor_face_normal)
    rho, _ = air_properties(setup.ambient_temp_c)
    rows = "\n".join(
        f"| `{name}` | {SPS30_LABELS[name]} | {v.minimum:g} | {v.maximum:g} | {v.distribution}{' (log)' if v.log else ''} |"
        for name, v in params.variables().items()
    )
    fan = (
        f"Mass-flow outlet, {rho * setup.fan_flow_m3s():.3e} kg/s (SPS30 fan {setup.fan_flow_lpm:g} L/min); DPM: trap"
        if setup.fan_enabled
        else "Wall; DPM: trap"
    )
    (folder / "README_FLUENT.md").write_text(
        f"""# SPS30 housing study -- ANSYS Fluent / Workbench package

Coordinates in metres, **wind along +x, z up** (the platform travels towards -x).
Housing bounding box: {np.round(low, 4).tolist()} to {np.round(high, 4).tolist()}.
Tunnel: {np.round(d_low, 3).tolist()} to {np.round(d_high, 3).tolist()}.

## Named selections

`inlet` (x min), `outlet` (x max), `side_neg` (y min), `side_pos` (y max), `top`,
`bottom`, `sensor` (the SPS30 intake face, centre {np.round(sensor, 4).tolist()},
looking along {normal.round(3).tolist()}), `housing` (every other housing face).

## Physics

* Pressure-based, steady; air constant density {rho:.4f} kg/m3.
* Viscous: **k-omega SST**.
* Discrete Phase: one-way (no interaction), water-liquid droplets,
  **Discrete Random Walk** on, surface or group injection upstream of the
  housing, diameter = parameter `droplet_diameter`.

## Boundary conditions

| Zone | Setting | DPM |
|---|---|---|
| inlet, side_neg, side_pos | Velocity inlet, components (speed cos(yaw), speed sin(yaw), 0) | escape |
| outlet | Pressure outlet 0 Pa | escape |
| top, bottom | Symmetry | reflect |
| housing | Wall, no slip | **trap** |
| sensor | {fan} | **trap** (failure count) |

The weep hole needs no boundary of its own: it opens to the outside air, which
is the 0 Pa ambient the specification asks for.

## Parameters

| This program | Meaning | Min | Max | Distribution |
|---|---|---|---|---|
{rows}

Workbench names: `speed`, `yaw`, `droplet_diameter` (inputs);
`face_velocity` (Facet Maximum of Velocity Magnitude on `sensor`),
`sensor_trap_count` and `injected` (DPM), `ux_integral` (Integral of |x-velocity|
on a plane x = {setup.chamber_plane_x_m:g} m bounded by the housing; the program
halves it into the exchange flow).

Export the Workbench design-point table as CSV and import it on the SPS30 housing
study tab; see docs/en/SPS30_STUDY_GUIDE.md (Czech: docs/cs/SPS30_STUDY_GUIDE.md).
""",
        encoding="utf-8",
    )
    return folder
