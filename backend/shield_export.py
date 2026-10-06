"""Package a radiation-shield study for ANSYS Fluent + Workbench/DesignXplorer.

The same study that runs here can be solved in Fluent: this writes

* ``shield_solid.step`` and ``fluid_domain.step`` -- the shield placed in the
  centre of the domain, and the air volume with the shield cut out, both in
  metres with z up and the wind along +x;
* ``design_points.csv`` -- the DoE, importable into the Workbench Parameter
  Set (or DesignXplorer's custom DoE) and, once solved, back into this
  program;
* ``fluent_setup.jou`` -- a Fluent TUI journal with the models, the named
  expressions used as Workbench input parameters and the boundary
  conditions;
* ``README_FLUENT.md`` -- the step-by-step Workbench/DesignXplorer procedure,
  including the settings the journal cannot make for you.

Nothing here runs Fluent; the journal is a template to check against the
Fluent version in use (TUI prompts shift between releases).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from core.shield_models import (
    STEFAN_BOLTZMANN,
    VARIABLE_LABELS,
    BottomMode,
    ShieldStudyParams,
)


def export_fluent_package(params: ShieldStudyParams, folder: Path | str) -> Path:
    """Write the Fluent/Workbench package for a study into ``folder``."""
    from backend.shield_study import design_of_experiments, write_design_points_csv

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    setup = params.setup
    geometry = write_geometry(params, folder)
    points = design_of_experiments(params)
    write_design_points_csv(folder / "design_points.csv", setup, points)
    (folder / "fluent_setup.jou").write_text(fluent_journal(params, geometry), encoding="utf-8")
    (folder / "README_FLUENT.md").write_text(readme(params, geometry, len(points)), encoding="utf-8")
    (folder / "study.json").write_text(
        json.dumps(params.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    return folder


def write_geometry(params: ShieldStudyParams, folder: Path) -> dict:
    """The placed shield and the fluid volume as STEP files, in metres."""
    import gmsh
    import numpy as np

    from backend.gmsh_session import gmsh_session
    from backend.shield_cfd import ShieldCfdError, resolve_shield_step

    setup = params.setup
    source, scale = resolve_shield_step(setup, folder)
    length, width, height = setup.domain_size_m
    with gmsh_session("shield_export"):
        occ = gmsh.model.occ
        solids = [e for e in occ.importShapes(str(source)) if e[0] == 3]
        if not solids:
            raise ShieldCfdError(f"{source.name} contains no solid")
        if scale != 1.0:
            occ.dilate(solids, 0.0, 0.0, 0.0, scale, scale, scale)
        occ.synchronize()
        low = np.full(3, np.inf)
        high = np.full(3, -np.inf)
        for dim, tag in solids:
            box = gmsh.model.getBoundingBox(dim, tag)
            low = np.minimum(low, box[:3])
            high = np.maximum(high, box[3:])
        centre = np.array([0.0, 0.0, 0.5 * height])
        occ.translate(solids, *(centre - 0.5 * (low + high)))
        occ.synchronize()
        gmsh.write(str(folder / "shield_solid.step"))
        box = occ.addBox(-0.5 * length, -0.5 * width, 0.0, length, width, height)
        occ.cut([(3, box)], solids, removeTool=True)
        occ.synchronize()
        gmsh.write(str(folder / "fluid_domain.step"))
    size = (high - low).tolist()
    monitor = [c + o for c, o in zip(centre.tolist(), setup.thermometer_xyz_m)]
    if source.parent == folder and source.name == "radiation_shield.step":
        source.unlink(missing_ok=True)
    return {"centre": centre.tolist(), "size": size, "monitor": monitor}


def _k(value: float) -> str:
    return f"{value:.4f}"


def fluent_journal(params: ShieldStudyParams, geometry: dict) -> str:
    """A Fluent TUI journal for the baseline case, parameterised for Workbench."""
    setup = params.setup
    ambient = setup.ambient_temp_k()
    x, y, z = geometry["monitor"]
    roof = setup.bottom_mode is BottomMode.ROOF_TEMPERATURE
    lines = [
        "; AeroThermalStudio -- radiation shield, Fluent setup journal",
        "; Zones expected (Named Selections in Workbench Meshing):",
        ";   inlet, outlet, top, bottom, sides, shield (walls), fluid, and",
        ";   solid-shield if the shield is meshed as a solid (conjugate).",
        "; Check every prompt against your Fluent version before relying on it.",
        "",
        "; ---- Models: pressure-based steady, energy, standard k-epsilon, DO ----",
        "/define/models/solver/pressure-based yes",
        "/define/models/steady yes",
        "/define/models/energy? yes no no no yes",
        "/define/models/viscous/ke-standard? yes",
        "/define/models/radiation/do-model? yes",
        "/define/operating-conditions/gravity yes 0 0 -9.81",
        f"/define/operating-conditions/operating-temperature {_k(ambient)}",
        "",
        "; ---- Workbench input parameters (named expressions) ----",
        f'/define/named-expressions/add "wind_speed" definition "{setup.wind_speed_ms:g} [m/s]" input-parameter yes quit',
        f'/define/named-expressions/add "solar_flux" definition "{setup.solar_flux_w_m2:g} [W/m^2]" input-parameter yes quit',
        f'/define/named-expressions/add "bottom_flux" definition "{setup.baseline_bottom_flux():.2f} [W/m^2]" input-parameter yes quit',
        f'/define/named-expressions/add "t_inlet" definition "{_k(ambient)} [K]" quit',
        "",
        "; ---- Pressure-velocity coupling ----",
        "/solve/set/p-v-coupling 24",
        "",
        "; ---- Thermometer monitor point and the objective ----",
        f"/surface/point-surface thermometer {x:.5f} {y:.5f} {z:.5f}",
        '/define/named-expressions/add "delta_t" definition "Average(StaticTemperature,[\'thermometer\'],Weight=\'none\') - t_inlet" output-parameter yes quit',
        "",
        "; ---- Boundary conditions (expressions drive the parameters) ----",
        '/define/boundary-conditions/set/velocity-inlet inlet () vmag yes "wind_speed" temperature no '
        f"{_k(ambient)} quit",
        "/define/boundary-conditions/set/pressure-outlet outlet () gauge-pressure no 0 quit",
        "/define/boundary-conditions/set/symmetry sides () quit",
        "; SUN: Models > Radiation > Solar Load > Solar Ray Tracing (set in the GUI):",
        ";      sun direction (0, 0, -1), direct solar irradiation = solar_flux.",
        "; TOP: zero-shear wall, heat flux 0, DO semi-transparent with Diffuse",
        ";      Irradiation = sky long-wave; not a solar-ray-tracing participant.",
        "; BOTTOM:",
    ]
    if roof:
        lines += [
            f"/define/boundary-conditions/set/wall bottom () thermal-bc yes temperature "
            f"temperature no {_k(setup.bottom_temperature_k)} quit",
            f";   vehicle roof at {setup.bottom_temperature_k:g} K, internal emissivity "
            f"{setup.roof_emissivity:g} (emits {setup.roof_flux_for(setup.bottom_temperature_k):.0f} W/m2).",
            ";   In a study drive the temperature from bottom_flux: "
            f"(bottom_flux/({setup.roof_emissivity:g}*5.670374e-8 [W/m^2/K^4]))^0.25.",
        ]
    else:
        lines += [
            ";   ground: semi-transparent zero-shear wall with Diffuse Irradiation = bottom_flux",
            ";   (upward long-wave), adiabatic.",
        ]
    lines += [
        f"; SHIELD walls: emissivity {setup.shield_emissivity:g}, solar absorptivity "
        f"{setup.shield_solar_absorptivity:g} (wall Radiation tab, solar ray tracing),",
        *_two_sided_journal(setup),
        f";   conductivity {setup.shield_conductivity_w_mk:g} W/(m K) for the solid zone.",
        "",
        "; ---- Initialise and solve ----",
        "/solve/initialize/hyb-initialization",
        "/solve/iterate 1500",
        "",
    ]
    return "\n".join(lines)


def _two_sided_journal(setup) -> list[str]:
    """Journal comments for a shield whose up and down faces differ."""
    if not setup.two_sided():
        return []
    optics = setup.side_optics()
    if setup.optics_orientation == "thermometer":
        return [
            ";   TWO-SIDED plates (by thermometer): split the shield wall into the",
            ";   faces looking INTO the gaps towards the thermometer and the outside",
            ";   faces (SpaceClaim named selections shield_inside / shield_outside).",
            f";   inside: solar absorptivity {optics['bottom'][0]:g}, emissivity {optics['bottom'][1]:g};",
            f";   outside: solar absorptivity {optics['top'][0]:g}, emissivity {optics['top'][1]:g}.",
        ]
    return [
        ";   TWO-SIDED plates: split the shield wall by face normal first, e.g.",
        ";   /mesh/modify-zones/sep-face-zone-angle shield 60",
        ";   then give the zones that look up (+z) solar absorptivity "
        f"{optics['top'][0]:g}, emissivity {optics['top'][1]:g},",
        ";   the zones that look down (-z) solar absorptivity "
        f"{optics['bottom'][0]:g}, emissivity {optics['bottom'][1]:g},",
        f";   and the edges {optics['side'][0]:g} / {optics['side'][1]:g}.",
    ]


def _two_sided_readme(setup) -> str:
    if not setup.two_sided():
        return ""
    optics = setup.side_optics()
    if setup.optics_orientation == "thermometer":
        return f"""
### Two-sided plates (inside / outside)

| Faces | Solar absorptivity | Emissivity |
|---|---|---|
| looking towards the thermometer (inside the gaps) | {optics['bottom'][0]:g} | {optics['bottom'][1]:g} |
| all other faces (outside) | {optics['top'][0]:g} | {optics['top'][1]:g} |

Make two named selections in SpaceClaim (`shield_inside`, `shield_outside`)
so Fluent gets two wall zones, and assign the values above. The program
classifies a face as inside when its outward normal points towards the
thermometer point.
"""
    return f"""
### Two-sided plates

The plates have different optics on their two sides:

| Faces | Solar absorptivity | Emissivity |
|---|---|---|
| looking up (+z, towards the sun) | {optics['top'][0]:g} | {optics['top'][1]:g} |
| looking down (-z, towards the ground) | {optics['bottom'][0]:g} | {optics['bottom'][1]:g} |
| edges (vertical) | {optics['side'][0]:g} | {optics['side'][1]:g} |

Fluent sets wall optics per face zone, so the `shield` wall has to be split:
either make two named selections in SpaceClaim / Meshing (`shield_top_side`,
`shield_bottom_side`; *Select -> by face normal*), or in Fluent
**Domain -> Mesh -> Separate -> Faces -> by angle** (TUI
`/mesh/modify-zones/sep-face-zone-angle shield 60`) and assign the values
above to the zones that look up and down. With a conjugate solid zone the
plate conducts between its two sides as it really does.
"""


def readme(params: ShieldStudyParams, geometry: dict, point_count: int) -> str:
    """Step-by-step instructions for Workbench, Fluent and DesignXplorer."""
    setup = params.setup
    variables = params.variables()
    rows = "\n".join(
        f"| `{name}` | {VARIABLE_LABELS[name]} | {v.minimum:g} | {v.maximum:g} | "
        f"{v.distribution.value} |"
        for name, v in variables.items()
    )
    fluent_names = {
        "wind_speed_ms": "wind_speed",
        "solar_flux_w_m2": "solar_flux",
        "bottom_flux_w_m2": "bottom_flux",
    }
    roof_line = (
        f"Wall (no slip), fixed temperature {setup.bottom_temperature_k:g} K (vehicle roof), "
        f"internal emissivity {setup.roof_emissivity:g} -> "
        f"{setup.roof_flux_for(setup.bottom_temperature_k):.0f} W/m2 upward"
        if setup.bottom_mode is BottomMode.ROOF_TEMPERATURE
        else f"Wall, specified shear 0, heat flux 0, DO: semi-transparent, diffuse "
        f"irradiation = `bottom_flux` ({setup.bottom_flux_w_m2:g} W/m2 ground long-wave)"
    )
    sky = setup.sky_flux_w_m2()
    x, y, z = geometry["monitor"]
    return f"""# Radiation shield study -- ANSYS Fluent / Workbench package

Generated by AeroThermalStudio. Coordinates in metres, **z up, wind along +x**.

## Files

| File | Use |
|---|---|
| `fluid_domain.step` | Air volume {setup.domain_size_m[0]:g} x {setup.domain_size_m[1]:g} x {setup.domain_size_m[2]:g} m with the shield cut out |
| `shield_solid.step` | The shield in place (for a conjugate solid zone) |
| `design_points.csv` | {point_count} design points ({params.doe.value.upper()}), import into the Parameter Set |
| `fluent_setup.jou` | Fluent TUI journal: models, named expressions, boundary conditions |
| `study.json` | The full study definition (open it again in AeroThermalStudio) |

## 1. Geometry and mesh (Workbench)

1. Geometry: import both STEP files; share topology between the shield and the air
   (Workbench SpaceClaim: *Share*), so the walls are conformal interfaces.
2. Named selections: `inlet` (x = {-0.5 * setup.domain_size_m[0]:g}), `outlet`
   (x = {0.5 * setup.domain_size_m[0]:g}), `bottom` (z = 0), `top`
   (z = {setup.domain_size_m[2]:g}), `sides` (y = +/-{0.5 * setup.domain_size_m[1]:g}),
   `shield` (the shield walls).
3. Mesh: inflation on `shield` (5-8 layers), face sizing 2-4 mm on the shield,
   body of influence round the shield and its wake.

## 2. Fluent physics

* Pressure-based, steady, coupled pressure-velocity; gravity (0, 0, -9.81).
* Energy on; **standard k-epsilon** with enhanced wall treatment.
* Radiation (long-wave): **Discrete Ordinates**, gray, theta/phi divisions 4 x 4.
* Sun: **Solar Load -> Solar Ray Tracing**, sun direction (0, 0, -1), direct
  solar irradiation = `solar_flux`, diffuse 0. Ray tracing shades the lower
  louvres and applies the shield's *solar* absorptivity
  ({setup.shield_solar_absorptivity:g}), which the gray DO model cannot tell
  apart from its long-wave emissivity ({setup.shield_emissivity:g}).
* Air: incompressible ideal gas (or Boussinesq) at {setup.ambient_temp_c:g} C.
* Shield: walls, heat flux 0 (radiative-convective equilibrium), internal
  emissivity {setup.shield_emissivity:g}; or a conjugate solid of
  conductivity {setup.shield_conductivity_w_mk:g} W/(m K).

{_two_sided_readme(setup)}
## 3. Boundary conditions

| Zone | Setting |
|---|---|
| inlet | Velocity inlet, magnitude = `wind_speed`, T = {setup.ambient_temp_k():.2f} K |
| outlet | Pressure outlet, 0 Pa gauge |
| sides | Symmetry |
| top | Wall, specified shear 0, heat flux 0, DO: semi-transparent, diffuse irradiation {sky:.0f} W/m2 (sky long-wave; 0 to leave it out); not a solar-ray-tracing participant |
| bottom | {roof_line} |
| shield | Wall, heat flux 0, internal emissivity {setup.shield_emissivity:g}, solar absorptivity {setup.shield_solar_absorptivity:g} (direct visible, direct IR, diffuse) |

## 4. Parameters

Input parameters (create them from the boundary-condition fields, *New Input
Parameter*, then rename them in the Parameter Set):

| This program | Meaning | Min | Max | Distribution |
|---|---|---|---|---|
{rows}

Workbench names: {", ".join(f"`{fluent_names[n]}`" for n in variables)}.

Output parameter: `monitor_temperature` -- a *Vertex Average* surface report of
static temperature, in K, on the point surface `thermometer`
({x:.4f}, {y:.4f}, {z:.4f}). This program subtracts the inlet temperature on
import. Objective: minimise it.

## 5. DesignXplorer

1. **Design of Experiments**: Central Composite Design, face-centred (matches
   `design_points.csv`), or *Custom* and import `design_points.csv`.
2. **Response Surface**: Genetic Aggregation or Full 2nd-Order Polynomials; check
   the goodness of fit and verification points.
3. **Six Sigma Analysis** (Monte Carlo on the response surface): the distributions
   above, 10 000+ samples; read the worst case and the probability that
   |delta_t| <= {params.tolerance_k:g} K.

## 6. Back into AeroThermalStudio

Export the design-point table from Workbench as CSV (columns named
`P1 - wind_speed`, `P4 - monitor_temperature [K]` etc. are recognised; a
temperature in C is converted), then in the Sensor tab -> Radiation shield
study -> *Import solved design points*. The full beginner's walk-through is
`docs/en/SHIELD_STUDY_GUIDE.md` (Czech: `docs/cs/SHIELD_STUDY_GUIDE.md`). The program fits its
own response surface and Monte Carlo to the Fluent numbers, so the two can be
compared directly.
"""
