# SPS30 housing study — a detailed guide from scratch

This guide takes you through a complete study of a housing for an **SPS30**
particulate sensor on a moving platform: from an estimate in seconds, through
a CFD solve of the air and the droplets in this program (SU2), to the same
solve in **Ansys Student** (Fluent with the Discrete Phase Model, Workbench,
DesignXplorer). It is written for someone who has never done CFD and barely
knows Ansys.

General terms (mesh, boundary condition, convergence, design point, response
surface, Monte Carlo…) are explained in the glossary of the [Radiation Shield
Study Guide, section 2](SHIELD_STUDY_GUIDE.md#2-glossary); only the extra
terms are here.

> **Being honest up front.** The program part has been verified with real
> SU2. The Ansys part follows the Fluent documentation but has not been
> clicked through step by step in Ansys Student by the author — dialogs
> differ between releases. Every step therefore comes with a **check**
> (section 7.8) that tells you it is set up correctly.

**Contents**

1. [What this is about](#1-what-this-is-about)
2. [Extra terms](#2-extra-terms)
3. [Plan](#3-plan)
4. [Preparation: software and the housing CAD](#4-preparation-software-and-the-housing-cad)
5. [Part A — an estimate in seconds](#5-part-a--an-estimate-in-seconds)
6. [Part B — CFD in this program](#6-part-b--cfd-in-this-program)
7. [Part C — Ansys Student step by step](#7-part-c--ansys-student-step-by-step)
8. [Part D — getting the most accurate data](#8-part-d--getting-the-most-accurate-data)
9. [Reading the results](#9-reading-the-results)
10. [When something goes wrong](#10-when-something-goes-wrong)
11. [Program vs Fluent](#11-program-vs-fluent)

---

## 1. What this is about

The SPS30 measures dust by drawing air in with a weak fan. It needs:

- **slow air at its intake** (below 1 m/s) — a fast stream would bias the
  reading or overpower the fan,
- **no water** — a droplet inside ruins it,
- **fresh air** — so it does not measure stale air trapped in the housing.

![The built-in SPS30 housing from outside and cut open](../images/sps30_housing_example.png)

*Left: the built-in 120 × 70 × 80 mm housing from outside, with the oncoming
air (here 20 m/s, yaw 10°) and one of the two side slits. Right: cut open
lengthwise — air comes in through the slit, drops through the plenum, has
to turn over the baffle, and only then reaches the sensor chamber and the
SPS30 intake (green); the purple arrow is the SPS30's own fan sucking air
in; water that gets in leaves through the weep hole. The tab draws the same
for your own STEP (**3D geometry**, button **Draw 3D geometry**), and so
does the assistant.*

The housing does this with three tricks:

1. **Static slits on the sides** instead of a forward scoop. The side of a
   moving box sees only static pressure, not the wind's ram pressure. Two
   slits facing each other balance out — a gentle cross-flow, no vacuum.
2. **A plenum** right behind the slits: the air spreads into a larger volume
   and slows down.
3. **A baffle** — the air turns sharply round it, but heavy droplets cannot
   make the turn, hit the baffle and drain through a **weep hole** in the
   floor.

For speeds of **5–35 m/s**, crosswind (**yaw ±20°**) and droplets from mist
(**10 µm**) to rain (**2000 µm**), the study finds:

- the highest air speed at the sensor face,
- the share of droplets that reach the sensor face (goal: zero),
- how much air is exchanged in the sensor chamber,
- and the **probability** that every goal is met (Monte Carlo).

> **What to expect.** Large droplets (hundreds of µm and up) are reliably
> separated. **Fine mist** (10–20 µm) in slowed air is so light that it
> follows the flow — inertia cannot stop it. If the study shows mist getting
> through, that is physics, not a calculation error; the fix is a
> hydrophobic membrane, a filter or a longer labyrinth.

---

## 2. Extra terms

| Term | Meaning |
|---|---|
| **Static pressure** | The air's pressure "sideways"; on the side wall of a moving body it does not include the ram of the wind. |
| **Ram / dynamic pressure** | The pressure of air hitting head-on (½ρV²). A forward-facing opening "catches" it. |
| **Yaw** | The angle the air comes from the side at (crosswind). With yaw one slit sees more wind than the other. |
| **DPM** | Fluent's *Discrete Phase Model* — tracks individual droplets in the air stream. |
| **Trap** | Droplet boundary condition: the droplet stops at the wall (and is counted). |
| **Escape** | The droplet leaves the calculation (inlet, outlet). |
| **Random walk (DRW)** | Random "nudging" of droplets by turbulent eddies — how small droplets spread. |
| **Stokes number** | A droplet's inertia against how fast the flow turns it. Small → the droplet goes with the air; large → it flies straight and hits. The threshold for an impactor is about 0.6. |
| **Penetration** | The share of the droplets that got inside the housing and reached the sensor face. |
| **Exchange flow** | How much air passes through the sensor chamber per minute (L/min). |

---

## 3. Plan

```
A  Estimate in the program (lumped model) .... 1 minute
B  CFD in the program (SU2 + droplets) ....... 1–2 days (runs unattended)
C  Ansys Student (Fluent + DPM) .............. 3–5 days of work + solving
D  Comparison and Monte Carlo ................ 1–2 hours
```

Recommended order: A → B (a mesh test on one point first) → C on the same
points → D comparison in the program.

---

## 4. Preparation: software and the housing CAD

- **The program** and **SU2 + MS-MPI**: [Tutorial](TUTORIAL.md), section 1
  (`Setup.bat`); check with **Help → Check environment**.
- **Ansys Student**: see the [Radiation Shield Study Guide, 4.2](SHIELD_STUDY_GUIDE.md#42-ansys-student-part-c-only)
  (installation, the ~1 million cell limit).

**The housing CAD** (or leave the field empty for the built-in housing):

- STEP, one **solid** with every internal space **cut out**: the slits, the
  plenum, the passage round the baffle, the sensor chamber and the weep
  hole. The program solves the air wherever there is no material.
- **The sensor face must be a separate flat face** (e.g. the front of a
  small block standing for the SPS30 intake). The program finds it from the
  centre, direction and size you enter.
- No screws, lettering or fillets under ~0.5 mm.
- Note: which way the platform travels in the CAD (e.g. −x), the centre of
  the sensor face, which way the face looks (into the chamber) and its size.

The built-in housing: 120 × 70 × 80 mm, travelling towards −x, 20 × 3 mm side
slits near the front, a baffle 58–61 mm from the front (15 mm gap at the
top), a 20 × 20 mm sensor face centred at (107, 0, 30) mm looking towards −x,
a 4 mm weep hole in the floor.

---

## 5. Part A — an estimate in seconds

1. Start the program → tab **Sensor Microclimate (BMP580)** → sub-tab **SPS30
   housing study**.
2. **Housing and sensor**: *Housing STEP* (empty = built-in), *Travel
   direction*, *Sensor face centre / looks / size*, *Chamber plane x* (the
   plane across the sensor chamber the exchange flow is measured on), *Model
   the SPS30 fan* and *Fan flow* (the default 0.3 L/min is an estimate; enter
   the real one if you know it).
3. **Tunnel, air and goals**: tunnel size in housing lengths (defaults are
   fine), air temperature, goals: *face velocity below* 1 m/s, *penetration
   at most* 0, *exchange flow at least* (0 = report only).
4. **Lumped model dimensions**: slit area and width, plenum and chamber
   cross-sections, baffle gap. For your own housing take them from the CAD.
5. **Uncertain inputs**: speed 5–35 m/s, yaw −20..20°, droplets 10–2000 µm
   (*Log* ticked = each decade of size equally likely).
6. **Run analytic study**.

The lumped model is **indicative** — it shows trends (when the face velocity
rises, which droplets get through), not final numbers.

### 5.1 One input at fixed steps (sweep)

**Sweep one input** steps one input (speed, yaw or droplet size) from *From*
to *To* in *Step*, with the others at the baseline values, and computes every
point directly with the lumped model — a chart and a table on the **Sweep**
tab, saved as `sweep.csv`. For the assistant: *"SPS30 sweep, droplets
10–200 µm every 10"* (action `sweep`). Don't read such tables off the
response surface — that is a fit, not the model.

---

## 6. Part B — CFD in this program

### 6.1 What the program does

1. Turns the housing so the wind blows along +x and puts it in a virtual
   tunnel (3 housing lengths ahead, 6 behind, 3 to the sides, up and down).
2. Meshes the air — outside and inside the housing (slits, plenum, chamber).
3. **SU2** solves the flow with the **SST k-ω** turbulence model (as the
   specification requires). Crosswind (yaw) turns the inlet velocity; the
   windward side wall of the tunnel is an inlet too, the leeward one an
   outlet. The SPS30 fan is a small extraction at the sensor face.
4. Then it **tracks droplets** of the design point's diameter: releases them
   ahead of the housing, applies air drag, gravity and random turbulent
   nudging, and stops each at the wall it meets. A droplet on the sensor
   face is a failure.
5. Measures the air speed 2 mm in front of the sensor face and the exchange
   flow in the chamber.

### 6.2 Settings (group *SU2 CFD + droplet tracking*)

| Field | Recommendation |
|---|---|
| *Mesh* | start with `coarse` (≈ 0.5 M cells) |
| *Iterations* | 2000 |
| *MPI ranks* | the number of physical cores |
| *Droplets per point* | 2000 (more = a more certain penetration; see 8.3) |
| *Discrete random walk* | on |
| *Points to solve now* | 1 for the mesh test, then *all* |

### 6.3 Mesh test and the full run

1. *Mesh* `coarse` → **Prepare CFD cases + Fluent package**.
2. *Points to solve now* = 1 → **Solve CFD design points (SU2 + droplets)**.
   The centre point DP0 is solved (20 m/s, 0°, ~140 µm). Note the face
   velocity and the exchange flow.
3. Repeat with `medium`. If the face velocity changes by < 10 %, `coarse`
   is enough.
4. Select that study → *Points to solve now* = all → Solve. 15 points is an
   overnight job (or run `cfd/run_design_points.bat` from the study folder).

### 6.4 Checks

- For every point the log shows `face … m/s, exchange … L/min, N of M
  droplets inside reached the sensor`.
- `cfd/points/DPx/`: `result.json` (numbers, including how many droplets
  ended on the housing walls, flew past or stayed unresolved) and
  `flow.vtu` (the whole field, e.g. for ParaView).
- **M (droplets inside)** should not be 0 — otherwise no droplet got into
  the housing and the penetration says nothing (normal for large droplets
  at 0° yaw).

---

## 7. Part C — Ansys Student step by step

### 7.0 Files from the program

After **Prepare CFD cases + Fluent package** → **Open study folder** →
the **`fluent/`** sub-folder:

| File | Use |
|---|---|
| `fluid_domain.step` | the tunnel with the housing "cut out", already turned (wind +x, up +z) |
| `housing_placed.step` | the housing alone in the same position |
| `design_points.csv` | the design points (open in Excel) |
| `README_FLUENT.md` | the numbers for your study (sensor face coordinates, fan flow, chamber plane) |

### 7.1 Workbench and geometry

1. Workbench → drag **Fluid Flow (Fluent)** onto the *Project Schematic* →
   save the project (a path without spaces or accented characters).
2. **Geometry → Import Geometry → Browse** → `fluid_domain.step`.
3. Double-click Geometry: one body; the tunnel box; the housing cavity
   inside. Measure the housing length (e.g. 120 mm). Close.

### 7.2 Mesh

In *Ansys Meshing* (double-click **Mesh**), face selection filter (*Face*):

**Named Selections** (select → right-click → *Create Named Selection*):

| Name | Faces |
|---|---|
| `inlet` | tunnel face x = min (upwind) |
| `outlet` | face x = max |
| `side_neg` | face y = min |
| `side_pos` | face y = max |
| `top`, `bottom` | faces z = max, z = min |
| `sensor` | **only** the sensor face (zoom into the chamber; *Hide* housing walls helps) |
| `housing` | every other housing face (Ctrl+A → Ctrl-click to remove the 6 tunnel faces and the sensor face) |

**Mesh settings** (click *Mesh*, *Details*): *Physics Preference* CFD,
*Solver* Fluent, *Element Size* 30 mm, *Growth Rate* 1.15.

- **Sizing** on `housing` and `sensor`: 1.5 mm.
- **Sizing** on the slit edges (filter *Edge*): 0.75 mm (at least 4 cells
  across the slit).
- **Body of Influence** (optional): a box round the housing, 3 mm.
- **Inflation** on `housing`: *First Layer Thickness* 0.05 mm, 3–5 layers,
  growth 1.2.

**Generate Mesh** → *Statistics* → the cell count must be below the student
limit; if not, coarsen (2 mm on the housing, 1 mm at the slits, fewer
layers). Quality: *Skewness* max < 0.95, *Orthogonal Quality* min > 0.1.
Close; in Workbench **Mesh → Update**.

### 7.3 Fluent — general and models

Double-click **Setup** → *Double Precision* → Start.

- **General**: Pressure-Based, **Steady**. Turn gravity on (Z = −9.81) — it
  matters for droplets. *Scale* → check the tunnel size.
- **Models → Viscous**: **k-omega (2 eqn)** → **SST** → OK.
- **Models → Discrete Phase**:
  - *Interaction with Continuous Phase*: off (few droplets, they do not
    affect the air),
  - *Tracking → Max. Number of Steps* 50 000, *Step Length Factor* 5,
  - *Physical Models*: nothing extra.
- **Materials**: air — constant density (default). Liquid: add
  **water-liquid** from the *Fluent Database*.

### 7.4 Boundary conditions

First the **input parameters** as *Named Expressions* (*Parameters &
Customization → Expressions → New*), each with **Use as Input Parameter**:

```
speed            = 20 [m/s]
yaw              = 0 [deg]
droplet_diameter = 0.0001 [m]
```

| Zone | Type | Settings | DPM (tab *DPM*) |
|---|---|---|---|
| `inlet`, `side_neg`, `side_pos` | velocity-inlet | *Specification Method*: **Components**; X = `speed*cos(yaw)`, Y = `speed*sin(yaw)`, Z = 0; turbulence 5 %, ratio 10 | escape |
| `outlet` | pressure-outlet | 0 Pa | escape |
| `top`, `bottom` | symmetry | — | reflect |
| `housing` | wall | no slip | **trap** |
| `sensor` | mass-flow-outlet (the fan) — mass flow from `README_FLUENT.md` (e.g. 6e-6 kg/s); or wall if you do not model the fan | **trap** |

> Side walls as *velocity inlets* with the full wind vector act as a
> prescribed outflow where the air leaves. For distant tunnel walls this is
> a common and accurate enough approach that works for positive and
> negative yaw without switching types.

The weep hole in the housing floor needs no boundary of its own — it opens
into the surrounding air at 0 Pa, which is exactly what the specification
asks for.

### 7.5 Droplets (injection)

1. **Results → Surfaces → Create → Plane**: *Point and Normal*, point (x =
   half a housing length ahead of the front, y = 0, z = housing middle),
   normal (1, 0, 0) → name `droplet_plane`.
2. **Models → Discrete Phase → Injections → Create**:
   - *Injection Type*: **surface**, *Release From Surfaces*: `droplet_plane`,
   - *Material*: water-liquid,
   - *Diameter*: `droplet_diameter` (expression/parameter),
   - *Velocity*: X = `speed*cos(yaw)`, Y = `speed*sin(yaw)`, Z = 0,
   - *Total Flow Rate*: 1e-6 kg/s (irrelevant with interaction off),
   - **Turbulent Dispersion** tab: tick **Discrete Random Walk Model**,
     *Number of Tries* 10, *Time Scale Constant* 0.15.

> The plane crosses the whole tunnel, so most droplets miss the housing.
> That is fine — the goal is that **none** lands on the sensor face.

### 7.6 Outputs

**a) Face velocity.** *Solution → Report Definitions → New → Surface Report
→ Facet Maximum* → *Velocity Magnitude* → surface `sensor` → tick **Create
Output Parameter** → name `face_velocity`.

**b) Droplets on the sensor.** After solving, *Results → Reports → Discrete
Phase → Summary* (or *Particle Fates*) shows how many droplets were trapped
on zone `sensor` and how many were released. If your version offers *Report
Definitions → New → DPM Report* with the trapped count per zone, make it the
output parameter `sensor_trap_count` (and `injected`). Otherwise copy the
numbers into the CSV by hand after each design point (section 7.9).

**c) Chamber exchange flow.**
1. *User-Defined → Field Functions → Custom*: `abs_ux` = **abs(X Velocity)**.
2. *Surfaces → Create → Iso-Surface*: *X-Coordinate* = the chamber plane from
   `README_FLUENT.md` → `xcut`.
3. *Surfaces → Create → Iso-Clip*: clip `xcut` by *Y-Coordinate* to the
   housing → `xcut_y`; then `xcut_y` by *Z-Coordinate* → `chamber_plane`.
4. *Report Definitions → New → Surface Report → Integral* → `abs_ux` on
   `chamber_plane` → output parameter `ux_integral` (m³/s; the program halves
   it and converts to L/min on import).

### 7.7 Solving

- *Methods*: **Coupled**, *Pseudo Time Method*, everything **Second Order**.
- *Residuals*: 1e-4 (continuity, velocities, k, ω).
- *Convergence Conditions*: `face_velocity`, 1e-4, 100 previous values.
- *Hybrid Initialization* → *Run Calculation* 2000 iterations.
- The DPM is computed at the end (or every N iterations, per the *DPM
  Iteration Interval*); afterwards look at the *Particle Tracks* (Results →
  Graphics → Particle Tracks, coloured by diameter).

### 7.8 Checks before running every point

| Check | How | What you must see |
|---|---|---|
| The wind is right | Contours → Velocity, plane z = housing middle | flow along +x (angled with yaw), flow round the housing |
| The slits are not scoops | Vectors on a plane through the slits | at 0° only a little flow through the slits; with yaw in through the windward one, out through the leeward one |
| The air inside is slow | Contours → Velocity inside | tenths of m/s in the plenum and chamber, not whole m/s |
| The fan pulls | Reports → Fluxes → Mass Flow Rate on `sensor` | the set flow |
| Droplets behave | Particle Tracks | large droplets fly straight / end on the baffle or walls; small ones follow the flow |
| Convergence | residuals, `face_velocity` | settled |
| Agreement with the program | the same point as DP0 of part B | the same order of face velocity and exchange flow |

### 7.9 All design points

1. Close Fluent (save). In Workbench open the **Parameter Set**.
2. Copy into the table from `design_points.csv`: `speed_ms` → `speed`,
   `yaw_deg` → `yaw`, `droplet_um` → `droplet_diameter` **converted to
   metres** (÷ 1 000 000, e.g. 141.4 µm → 0.0001414 m).
3. **Update All Design Points**.
4. Right-click the table → **Export Table Data as CSV**. If the droplet
   counts are not parameters, add `sensor_trap_count` and `injected`
   columns by hand in Excel.

On import the program recognises `speed`, `yaw`, `droplet_diameter` (in m,
mm or µm — from the unit in the header), `face_velocity`,
`sensor_trap_count` + `injected` (or `penetration` directly) and
`ux_integral` (or `exchange_flow_lpm`).

### 7.10 Optional: DesignXplorer

As for the shield ([7.10 there](SHIELD_STUDY_GUIDE.md#710-optional-designxplorer-inside-ansys)):
*Response Surface* (CCD face-centred) → *Six Sigma Analysis* with the input
distributions. Give the droplet diameter a *Lognormal* or *Uniform*
distribution over its range.

### 7.11 Importing into the program

Sub-tab **SPS30 housing study** → set the form as in Fluent → **Import
solved design points (CSV)…** → the program fits the surfaces and runs the
Monte Carlo.

---

## 8. Part D — getting the most accurate data

1. **Mesh independence** — face velocity and exchange flow change by < 10 %
   between two meshes. The slits need at least 4–6 cells across.
2. **Convergence** — `face_velocity` settled; at 5 m/s and 0° the flow
   inside may drift slowly — then average the last iterations.
3. **Number of droplets** — the goal is *zero*, so statistics decide: if none
   of 2000 droplets inside the housing reaches the face, the penetration is
   below ~0.15 % with 95 % confidence (the rule of 3/N). For a stricter proof
   raise *Droplets per point* (program) or *Number of Tries* / injection
   density (Fluent), and above all **solve the worst cases directly** (small
   droplets, large yaw).
4. **Re-solve the worst cases** — enter the *worst case* values as an extra
   design point; the response surface is least accurate in the corners of
   the ranges, and the penetration (zero almost everywhere, sharply non-zero
   somewhere) especially so. For penetration prefer *Radial basis* and more
   points (*Latin hypercube* 30+) over a polynomial.
5. **The fan** — the SPS30 flow affects the face velocity and the
   penetration of small droplets. Measure it (or find it in the
   documentation) and enter it.
6. **Program vs Fluent** — the same order of face velocity and exchange flow
   and the same verdict on droplets (which sizes get through) = a result you
   can trust.
7. **Validation** — rain/mist on a moving vehicle, water-indicator paper at
   the sensor face, and the SPS30 in the housing against a reference
   instrument.

**What the model leaves out:** droplet splashing and bouncing (everything is
"trap"), the water film running down (on the real baffle it flows to the
drain — make sure it cannot run to the sensor), evaporation, driving rain and
gusts.

---

## 9. Reading the results

| Card | Meaning |
|---|---|
| **Worst face velocity** | the highest face velocity over all random conditions (green = below the goal) |
| **Worst penetration** | the worst share of the droplets inside the housing that reach the face |
| **Lowest exchange flow** | the smallest air exchange (typically at 0° and low speed) |
| **Reliability** | the % of random conditions meeting **every** goal; the share per goal is below the cards |

Below the cards: the worst case of each output (speed, yaw, droplet
diameter), the **worst failing condition**, and notes (e.g. that fine mist
got through). The histogram switches between outputs; the red line is the
goal.

Example: "The face velocity always stays below 0.12 m/s (1 m/s goal met with
a wide margin). Droplets above 50 µm never get through; 10–20 µm mist at
small yaw gets through at a few per cent. Reliability 48 % — every failure is
fine mist → add a membrane in front of the sensor."

---

## 10. When something goes wrong

| Problem | Fix |
|---|---|
| *no housing face matches the sensor face* | check the sensor face centre, direction and size (CAD units converted to metres); the face must be a separate flat face |
| *cutting the housing out left N air volumes* | the CAD has a closed cavity with no way out — check the slits really go through the wall |
| meshing fails | a finer mesh; remove tiny edges in CAD |
| `0 of 0 droplets inside` | no droplet entered the housing — normal for large droplets; for small ones check the injection aims at the housing (yaw) |
| Fluent: mesh above the limit | larger housing cells (2 mm), 3 inflation layers, a smaller tunnel (2/4/2 lengths) |
| Fluent: droplets "incomplete" | raise *Max. Number of Steps*; small droplets circle for a long time in a slow plenum |
| Import: *lacks the column(s)* | rename the columns to `speed`, `yaw`, `droplet_diameter`, … |

---

## 11. Program vs Fluent

| | Program (SU2) | Ansys Fluent |
|---|---|---|
| Turbulence | SST k-ω | SST k-ω |
| Droplets | own tracking in the SU2 field: drag, gravity, random walk | DPM with Discrete Random Walk |
| Droplet at a wall | trap | trap (you set it) |
| SPS30 fan | prescribed suction velocity through the face | mass-flow outlet on `sensor` |
| Crosswind | turned inlet vector, windward wall inlet, leeward outlet | turned vector on the inlet and both sides |
| Mesh limit | none | ~1 million cells |
| Design points | automatic, overnight by script | Parameter Set → Update All |
| Surfaces + Monte Carlo | in the program | in the program (after import) or DesignXplorer |

---

## See also

- [User Guide](USER_GUIDE.md), section 9 — every field of the study.
- [MCP and AI Guide](MCP_AI_GUIDE.md), the `sps30_housing_study` tool.
- [Radiation Shield Study Guide](SHIELD_STUDY_GUIDE.md) — the general Ansys
  procedure in more detail (Workbench, mesh, parameters, DesignXplorer).
