# Radiation shield study — a detailed guide from scratch

This guide takes you through a complete radiation-shield study: from a first
estimate in seconds, through a CFD solve in this program (SU2), to the same
solve in **Ansys Student** (Fluent + Workbench + DesignXplorer) and a
comparison of the two. It is written for someone who has **never done CFD**
and barely knows Ansys — every term is explained, and every step says where
to click and what you should see.

> **Being honest up front.** The program part (A, B) has been verified with
> real SU2. The Ansys part (C) follows the Fluent documentation and
> experience with Fluent, but has not been clicked through step by step in
> Ansys Student by the author — dialog layouts shift a little between
> releases. That is why every Ansys step comes with a **check** that tells
> you it is set up correctly even if the dialog looks slightly different
> (section 7.8).

**Contents**

1. [What this is about — physics without equations](#1-what-this-is-about--physics-without-equations)
2. [Glossary](#2-glossary)
3. [Plan and how long it takes](#3-plan-and-how-long-it-takes)
4. [Preparation: software and the shield CAD](#4-preparation-software-and-the-shield-cad)
5. [Part A — a first estimate in seconds](#5-part-a--a-first-estimate-in-seconds)
6. [Part B — CFD in this program (SU2)](#6-part-b--cfd-in-this-program-su2)
7. [Part C — the same solve in Ansys Student](#7-part-c--the-same-solve-in-ansys-student)
8. [Part D — getting the most accurate data](#8-part-d--getting-the-most-accurate-data)
9. [Reading the results](#9-reading-the-results)
10. [When something goes wrong](#10-when-something-goes-wrong)
11. [Program vs Fluent: the differences](#11-program-vs-fluent-the-differences)

---

## 1. What this is about — physics without equations

A thermometer in the sun does not measure the air, it measures **itself** —
and the sun heats it. So it goes inside a **radiation shield**: a stack of
white plates (louvres) that lets air through but keeps the sun off the
thermometer.

The shield is not perfect either:

![The built-in shield cut open along the wind, and in its domain](../images/radiation_shield_example.png)

*Left: the built-in 20 cm shield cut open along the wind — six louvre plates
on four posts, the thermometer (red) in the middle, the sun (yellow) from
above, long-wave radiation (orange) from below, the wind (blue) from the
left. Right: the shield in the middle of the 2 × 2 × 1.44 m air domain. The
tab draws the same picture for your own STEP in **3D geometry** (button
**Draw 3D geometry**), and the assistant shows it too.*

- **Sun from above** heats the top plate; the warm plate heats the air
  flowing past it towards the thermometer.
- **Radiation from below** — long-wave (thermal) radiation from the ground,
  or the much stronger radiation of a **sun-baked car roof** (70 °C) — heats
  the lower plates.
- **Wind** carries heat away. The weaker the wind, the warmer the plates and
  the more warm air reaches the thermometer.
- The other way round: at night (or over cold ground) the plates **radiate**
  to the sky and can be **colder** than the air.

The result is a **measurement error ΔT** = air temperature at the
thermometer − true air temperature. The study finds:

1. how big ΔT is in typical conditions,
2. the **worst possible** ΔT (light wind, strong sun, hot roof),
3. the **probability** that the error stays below a chosen limit (e.g. 0.5 K).

There are infinitely many conditions, so the study works cleverly (the
methodology of the reference paper, *Atmosphere 2026, 17(3), 272*):

1. Only **15 chosen combinations** of wind, sun and bottom radiation are
   solved (the *design points*).
2. A smooth **function** (the *response surface*) is fitted through them; it
   estimates ΔT for any combination.
3. **Thousands of random conditions** are tried on that function (*Monte
   Carlo*) — a fraction of a second, since nothing is simulated any more.

---

## 2. Glossary

| Term | Meaning |
|---|---|
| **CFD** | *Computational Fluid Dynamics* — computing air flow and heat transfer. |
| **Domain** | The box of air round the shield that is computed. Here 2.0 × 2.0 × 1.44 m. |
| **Mesh** | The domain split into small cells (tetrahedra, prisms). Velocity and temperature are computed in each. More cells = more accurate but slower. |
| **Cell** | One piece of the mesh. Near the shield plates cells must be small (millimetres); far away they can be large (decimetres). |
| **Boundary condition** | What happens at the domain's faces: where air enters (*inlet*), where it leaves (*outlet*), what the ground and ceiling do. |
| **Solver** | The program that solves the equations on the mesh: SU2 (free, used by this program) or Fluent (Ansys). |
| **Iteration** | The solver approaches the answer step by step. Typically hundreds to thousands of steps. |
| **Convergence** | The answer no longer changes with more iterations. Only then is it valid. |
| **Residual** | How much the equations are still "off". It should fall; the residual plot is the main sign of convergence. |
| **Turbulence model** | A simplified description of eddies in the air. The specification asks for *standard k-ε*; SU2 does not have it and uses SST. |
| **Radiation model** | Computes radiation between surfaces and surroundings. In Fluent *Discrete Ordinates (DO)* and *Solar Load*; in the program its own ray method. |
| **y+** | Dimensionless thickness of the first cell at a wall. For the model in this guide it should be about 1 (at most ~5). |
| **Design point (DP)** | One combination of inputs (wind, sun, bottom radiation) and its computed ΔT. |
| **DoE** | *Design of Experiments* — the plan of which design points to solve. CCD = 15 points (corners, face centres and the centre of the "cube" of ranges). |
| **Response surface** | A smooth function through the design points that estimates ΔT between them. |
| **LOO error** | *Leave-one-out*: how well the function predicts a point it was not fitted to. The honest accuracy figure. |
| **Monte Carlo** | Trying thousands of random conditions on the response surface → statistics, worst case, reliability. |
| **Reliability** | The share of random conditions in which \|ΔT\| is within the chosen tolerance. |
| **Mesh independence** | Showing that a finer mesh no longer changes the answer noticeably. Without it you do not know whether you are computing physics or mesh error. |

---

## 3. Plan and how long it takes

```
A  Analytic estimate in the program .... 1 minute     → learn the trends
B  CFD in the program (SU2) ............ 1 day (runs overnight)
C  CFD in Ansys Student (Fluent) ....... 2–4 days of work + solving
D  Comparison, checks, Monte Carlo ..... 1–2 hours
```

You do not have to do both B and C. The **most accurate** result, though,
comes from two independent CFD routes that agree (section 8). Recommended
order:

1. **A** — get to know the program and how ΔT behaves.
2. **B** — a mesh-independence check on one point, then all 15 points.
3. **C** — Fluent on the same 15 points (the program prepares the geometry
   and the table of points for you).
4. **D** — import both into the program and compare.

---

## 4. Preparation: software and the shield CAD

### 4.1 This program

Install it as in the [Tutorial](TUTORIAL.md), section 1 (`Setup.bat`). Part B
also needs **SU2 and MS-MPI** — `Setup.bat` installs them; check with
**Help → Check environment** (both must be found).

### 4.2 Ansys Student (part C only)

1. Download the free installer from the *Ansys Student* page (ansys.com →
   Academic → Students). You need 64-bit Windows 10/11; 16 GB RAM is a
   sensible minimum, and about 40–50 GB of disk.
2. During installation keep **Fluids** (Fluent) and **Workbench** ticked.
3. **Student limit:** flow problems are limited in size (about **1 million**
   cells/nodes; the exact figure changes between releases — check the Ansys
   Student page). This guide works within it.
4. The student licence may also limit the number of CPU cores Fluent uses.
   That only makes it slower.

### 4.3 The shield CAD

Either use the **built-in shield** (20 × 20 × 20 cm, 6 plates, 4 posts —
leave *Shield STEP* empty), or your own model. What the program does with
your file, so you know what it needs:

1. it reads every **solid** from the STEP (surfaces and curves are ignored),
2. scales it to metres,
3. **moves it so its bounding-box centre sits in the middle of the domain**
   (the CAD origin does not matter),
4. cuts it out of the 2 × 2 × 1.44 m air box and meshes the air around it,
5. casts radiation rays from every face of it.

It does **not** rotate the model — the orientation in the file is used as is.

**Checklist for your STEP file**

| Requirement | Why | What happens otherwise |
|---|---|---|
| Format `.step` / `.stp`, exported as a **solid** (body), not surfaces/a shell | only solids are cut out of the air | *"contains no solid"* |
| **Wind along +x, up along +z** | the inlet is at x min, the sun comes along −z, the ground is at z min | the sun shines on the side, the wind blows from the wrong side — wrong numbers, no error |
| The plates and posts **joined into one solid**, or separate solids that **touch or do not overlap** (no two bodies sharing the same volume) | overlapping bodies give double faces and a broken mesh | mesh failure |
| **No closed cavity** — every air space must connect to the outside air (a hollow, sealed post or a closed box inside is not allowed; make posts solid) | the air must be one connected volume | *"left N air volumes"* |
| **No thermometer, holder or cable** in the model (unless you want to study it) | the thermometer is only a point where the air temperature is read | — |
| Shield **smaller than half the domain** in every direction (i.e. < 1 × 1 × 0.72 m) | room for the flow round it | *"too large for the domain — check the CAD units"* |
| Units: the program reads them from the file and checks them against the size; if it gets it wrong, set *Scale to metres* (0.001 = millimetres, 0.01 = cm) | | a 200 mm shield read as 200 m → the error above |
| Plates at least ~1.5 mm thick, air gaps **≥ ~3 mm**, no details under ~1 mm (screws, lettering, small fillets, snap hooks) | the program measures the narrowest air gap and sizes the cells near the shield to put 3 (coarse), 4 (medium) or 6 (fine) cells across it, and at least 20/30/45 along the shield — the log says what set the size. A smaller gap means many more cells | very narrow gaps → a huge mesh (over the Ansys Student limit) or a failing one |
| Under ~1 million cells for Ansys Student — keep the model simple | Student licence limit | Fluent refuses the mesh |

**Two-sided plates.** A plate can have different surfaces on its two sides
— shiny aluminium facing the sun and black paint underneath, say. Set
*Top side absorptivity / emissivity* (faces that look up) and *Bottom side
absorptivity / emissivity* (faces that look down); left at *auto* they use
the *Shield* values. Edges take the mean. The analytic model, the SU2 ray
casting and the Fluent package (README: how to split the shield wall by
face direction) all use them. For shiny aluminium use about 0.15 / 0.1, for
black paint about 0.95 / 0.9.

*Two-sided optics: Outside / towards the thermometer* (setup
`optics_orientation: "thermometer"`) splits the faces by the thermometer
instead of up/down: *Top side* = the outside faces, *Bottom side* = the
faces looking into the gaps towards the thermometer (a face whose normal
points towards the thermometer point). Black inside and shiny aluminium
outside is top 0.15 / 0.1, bottom 0.95 / 0.9. SU2 applies it when the
CFD study is prepared, so prepare a new one after changing it. The
analytic model only exchanges heat with the surroundings through the
outside, so there the inside optics do not change ΔT.

**Sky long-wave.** The atmosphere (water vapour, CO2, clouds) sends long-wave
radiation down; a real sky gives 250–420 W/m², never 0. Leave *Sky
long-wave* on *auto* and the program computes it from the air temperature:
L↓ = ε σ T_air⁴, with the clear-sky emissivity of Idso–Jackson
ε_clear = 1 − 0.261 exp(−7.77·10⁻⁴ (T − 273)²) (≈ 339 W/m² at 20 °C,
377 W/m² at 25 °C) and cloud cover N: ε = ε_clear + N (0.98 − ε_clear).
The *Sky* combo has presets:

| Sky | L↓ |
|---|---|
| Clear (model) | ≈ 340 W/m² at 20 °C |
| Partly cloudy (N 0.5) | ≈ 375 W/m² at 20 °C |
| Overcast (N 1) | ≈ 410 W/m² at 20 °C |
| Summer day | 330 W/m² |
| Conservative clear | 280 W/m² |
| Dry clear night | 250 W/m² |

0 W/m² simulates deep space at 0 K: the top plate is over-cooled and ΔT
comes out strongly negative. Below 220 W/m² the form shows the value in red
and every result carries a warning (`sky_warning`, a CFD point note).
A realistic summer-day test: solar 900–1000, sky 320–340, ground 420–480 W/m².

**Heated plate under the shield** (*Bottom: Heated plate*, setup
`bottom_mode: "heated_plate"`). The domain floor is a sheet heated by the
sun — e.g. a grey-painted aluminium sheet on a roof. Its temperature is
solved at every wind and sun from its own balance,
α_p S + ε_p L↓ = ε_p σ T⁴ + h (T − T_air)·(1 or 2 sides), with h the flat-plate
forced convection over its 2 m length blended with natural convection.
The floor is then held at that temperature in SU2, so the warm boundary
layer that grows over the sheet and reaches the shield is solved; the
shield receives the sheet's long-wave (ε_p σ T⁴ + (1 − ε_p) L↓) and the sun it
reflects ((1 − α_p) S). *Shield above floor* (`shield_clearance_m`) sets the
gap to the shield's base, e.g. 0.04 m. Defaults: α_p 0.65, ε_p 0.90
(grey paint), underside insulated (the hotter case). Not modelled: the
shield's shadow on the sheet and the sheet's cooler leading edge.

**Grid of exact points.** *Design of experiments: Grid* solves exactly the
values typed in each input's *Grid levels* column (comma-separated), every
combination: wind `0.5, 1, 2, 5, 10` and sun `500, 1000` give 10 points.
An input with an empty column stays at its baseline (one level; the
response surface then ignores it). Assistant: `study.doe: "grid"`,
`variables.<name>.levels`. Renders of a study with a roof or heated plate
draw the floor under the shield with its temperature and the gap, and the
caption gives the thermometer's height above it.

**Material.** The *Material* combo fills the conductivity and both sides'
optics at once (setup `shield_material`): *Aluminium* (k 167 W/(m K),
shiny 0.15 / 0.1 outside, black 0.95 / 0.9 inside), *ABS print* (k 0.17,
white ABS 0.25 / 0.90 outside, black ABS 0.95 / 0.92 inside) and *PETG
print* (k 0.20, the same colours) and *ABS print, aluminium tape outside*
(k 0.17, shiny tape 0.15 / 0.05 outside, black ABS inside). White paint and
plastic are white only to the sun: in the thermal infrared they are nearly
black (ε ≈ 0.9) and absorb a hot roof's or plate's long-wave; shiny metal
tape (ε ≈ 0.05, more when dirty or aged) reflects it. The two sides are split by the
thermometer. Any value can be edited afterwards.

**Radiation between the plates** (CFD option, `plate_radiation`, on by
default). The plates exchange long-wave radiation across the gaps: grey
diffuse surfaces, view factors from the same ray casting as the sky and
ground, the radiosity balance solved every pass. Off, each surface
radiates only to the sky and the ground and the part of its view filled by
other plates acts as a mirror. Prepare the CFD study again for studies made
before this option existed.

**Pictures after a solve.** *Solve CFD design points* draws an overview
(air temperature, air speed with arrows, streamlines, wall temperature) of
the baseline point and of the point with the largest |ΔT| and shows it on
the 3D geometry tab and in Graphics; untick *Draw overview pictures after
solving* to skip it.

**The assistant uses the shield you have open.** Whatever this tab shows —
your STEP, the optics, the ranges — is what the assistant's study tool
starts from when you do not name a study; every reply says so
(`setup_source`) and lists what it actually used (`applied_setup`,
`applied_optics`). So "run the analytic study on my shield" means this
shield, and its picture is your model, not the built-in one.

**Two fields must match your model**, because the analytic model and the
monitor point do not read them from the file:

- *Shield size* — the outer size in metres (x, y, z) — and *Plates*: the
  analytic model (Part A) and the sweep use them; the CFD uses the real
  geometry. When you pick a file, the program fills *Shield size* from it
  (and *Plates* from the number of separate solids, if there are several) —
  check them.
- *Thermometer* — the sensor position **relative to the centre of the
  shield's bounding box** (x along the wind, z up), in metres. With a shield
  whose plates are not symmetric top to bottom, the centre of the box is not
  necessarily where the sensor sits — measure it in CAD.

**Check it before you mesh:** press **Draw 3D geometry** (or ask the
assistant for a *preview*). The picture shows your shield cut open with the
thermometer (red point), the sun from above and the wind from the left —
if the plates stand on their side or the red point is in a plate, fix it
before spending hours on CFD. A thermometer point inside the material is
also flagged in red on the picture, and *Prepare CFD* refuses it.

Tips for a good model: one body made with *Combine/Union* in CAD; plates as
simple flat (or slightly conical) rings; the top plate solid; a material
thickness true to the real shield (it matters for conduction in Fluent).

---

## 5. Part A — a first estimate in seconds

1. Start the program (`AeroThermalStudio.bat`).
2. Tab **Sensor Microclimate (BMP580)** → sub-tab **Radiation shield study**.
3. Top left, **Shield and domain**:
   - *Shield STEP*: empty (built-in shield), or **Browse** for your file.
   - *Shield size*: the shield's size in metres (a STEP file's real size is
     used for meshing; this field feeds the analytic model).
   - *Domain*: 2.0 / 2.0 / 1.44 (the specification).
   - *Thermometer*: position **relative to the shield centre**, in metres;
     0 / 0 / 0 = the very centre.
4. **Baseline condition and surfaces**:
   - *Inlet air*: air temperature, e.g. 25 °C.
   - *Top solar*: 1000 W/m².
   - *Bottom*: **Ground: long-wave flux** (300 W/m²), or **Vehicle roof:
     fixed temperature** (343 K = 70 °C).
   - *Shield solar absorptivity*: how much sunlight the white surface
     absorbs. New white plastic ≈ 0.2; greyed or dirty 0.3–0.4. **Of all the
     material properties this one moves the result most** — see section 8.
   - *Shield emissivity*: 0.9 for ordinary plastics and white paint.
   - *Sky long-wave*: *auto* estimates the sky's radiation from the air
     temperature. To reproduce the specification exactly (sun only from
     above), set 0.
5. **Uncertain inputs** — the ranges of the three variables (defaults from
   the specification): wind 0.5–5 m/s, sun 800–1200 W/m², bottom radiation
   300–800 W/m². In roof mode the bottom radiation sets the roof temperature
   (750 W/m² ≈ 343 K).
6. **Design exploration**: keep *Central composite* (15 points), *Full
   quadratic*, 10 000 samples, tolerance 0.5 K.
7. Click **Run analytic study**.

Within a second you get result cards, a histogram and a plot of ΔT against
wind. The analytic model is **simplified** (one temperature for the whole
shield) — good for understanding trends and quickly comparing variants (what
a darker finish does, say), **not** for the final number.

### 5.1 One input at fixed steps (sweep)

To see how ΔT changes with **one** input — wind from 0.2 to 5 m/s every
0.2 m/s, say — use the box **Sweep one input**: choose the input, *From*,
*To* and *Step*, and press **Run sweep**. The other inputs stay at the
*Baseline condition* values. The **Sweep** tab shows a chart and a table of
every point, saved as `sweep.csv` (and `sweep.png`) in the program's
`sweeps` folder. For the assistant: *"radiation shield sweep, wind 0.2–5 m/s
every 0.2"* (action `sweep`).

Every point of a sweep is computed **directly** by the model. Never read
such a table off the response surface: a quadratic surface fitted on 15
points on 0.5–5 m/s gives 0.62 K at 0.2 m/s instead of 1.14 K and invents a
minimum and negative values around 3.6 m/s.

![Wind sweep, sun 1000 W/m², bottom 550 W/m²](../images/radiation_shield_wind_sweep.png)

---

## 6. Part B — CFD in this program (SU2)

### 6.1 What the program does

- Builds a **mesh** of the air round the shield (tetrahedra, fine at the
  plates, coarse at the domain walls, refined behind the shield where the
  warmed air goes).
- Computes the **radiation with rays**: from every shield facet it casts
  rays to find whether the sun reaches it (the top plate shades the lower
  ones) and how much sky and ground it sees.
- For each design point runs **SU2** (incompressible flow + energy + SST
  turbulence + gravity) and reads the air temperature at the thermometer.
- **Heat conduction in the plates** (*Heat conduction in the plates*, on by
  default): the shield bodies get a solid mesh too and the program solves
  conduction in them, coupled to SU2 pass by pass — SU2 holds the walls at
  the plates' temperature and reports the heat the air takes; the plates
  answer with their new temperature from the sun and long-wave they
  absorb, their own emission and that heat. Without it every surface facet
  is an independent wall, and sun absorbed on the top of an aluminium
  plate never reaches the thermometer (a study run that way shows the
  same ΔT for 800 and 1200 W/m² of sun).
- The passes stop as soon as the shield changes by less than 0.05 K
  (*Passes (max)*, 8 by default).

### 6.2 Settings

In the **SU2 CFD of the design points** group:

| Field | Recommendation |
|---|---|
| *Mesh* | start with `coarse` (≈ 0.2 M cells), see 6.3 |
| *Turbulence model* | SST |
| *Gravity and natural convection* | **on** (natural convection dominates in light wind) |
| *Radiation passes* | 3 (stops early once the wall temperatures settle) |
| *Iterations, first pass* | 1500 |
| *Iterations, later passes* | 600 |
| *Radiation markers* | 8 |
| *Rays per facet* | 64 |
| *MPI ranks* | the number of **physical** CPU cores (e.g. 4 or 8) |
| *Points to solve now* | see 6.3 |

### 6.3 First, a mesh test on one point

You cannot know in advance how fine a mesh is enough. So:

1. *Mesh* = `coarse` → **Prepare CFD cases + Fluent package**. A new study
   appears in the *Study* list at the top.
2. *Points to solve now* = **1** → **Solve CFD design points (SU2)**. Only
   the centre point DP0 is solved. The log at the bottom shows progress and
   ends with `1 of 15 design points solved`. Note DP0's ΔT (points table).
3. Repeat with *Mesh* = `medium`, and `fine` if you have time.

| Mesh | ΔT (DP0) | Change from previous |
|---|---|---|
| coarse | … | — |
| medium | … | … |
| fine | … | … |

**Rule:** if ΔT changes by less than ~0.03 K (or less than 10 %) between two
meshes, the coarser one is enough. If it changes more, use the finer one.

### 6.4 All 15 points

Select the study with an adequate mesh, *Points to solve now* = **all** →
**Solve CFD design points (SU2)**. The already solved DP0 is not repeated.

- One point on a coarse mesh takes tens of minutes to an hour depending on
  the computer; 15 points is an overnight job.
- The program does not need to stay open: after preparation the study
  folder holds **`cfd/run_design_points.bat`**, which solves every unsolved
  point (on any computer with the program installed). **Open study folder**
  opens it.
- If the run is interrupted, start it again — it continues from the
  unsolved points.

When all points are solved the program fits the response surface and runs
the Monte Carlo by itself.

### 6.5 Checking the SU2 results

- For every point the log must show `pass 2 mean wall temperature change`
  below ~0.05 K (the radiation has settled).
- `cfd/points/DPx/` holds `result.json` (the numbers) and `flow.vtu` (the
  whole field, opens in e.g. ParaView).
- ΔT should behave sensibly: fall with wind, rise with sun and bottom
  radiation. A point off the trend is suspect.
- The same checks are written into each point's `result.json` and returned
  to the assistant (`point_results`, `checks`):
  - `wall_changes_k` — the wall-temperature change of every radiation
    pass; `radiation_settled` is false when the last one is still above
    0.05 K (raise *Radiation passes*).
  - `oscillating` — the residuals of the last quarter of the iterations
    swing without falling: the flow is unsteady (typical at low wind) and
    the ΔT is an estimate of its time average.
  - `converged` — SU2 met its residual target before the iteration limit.
- *Prepare CFD* reports the narrowest air gap it found and the cell size it
  chose near the shield; for a mesh test solve DP0 on two resolutions and
  compare its ΔT (difference < 0.03 K or 10 % → the coarser mesh is enough).

---

## 7. Part C — the same solve in Ansys Student

### 7.0 Get the files from the program

After **Prepare CFD cases + Fluent package**, the study folder (**Open study
folder**) has a **`fluent/`** sub-folder:

| File | Use |
|---|---|
| `fluid_domain.step` | the air, 2 × 2 × 1.44 m, with the shield "cut out" — this is what you mesh |
| `shield_solid.step` | the shield alone (only for the advanced solid-zone variant) |
| `design_points.csv` | the 15 design points — open in Excel |
| `README_FLUENT.md` | a short summary of the settings with the numbers for your study |
| `fluent_setup.jou` | a journal (script) for advanced users — optional |

Coordinates: **x** along the wind (inlet at x = −1 m, outlet at x = +1 m),
**y** sideways, **z** up (ground z = 0, ceiling z = 1.44 m). The thermometer
is at the shield centre, by default **(0, 0, 0.72)** m.

### 7.1 Workbench project

1. Start **Workbench** (Start → Ansys 20xx R… → Workbench).
2. From **Toolbox → Analysis Systems** on the left, drag **Fluid Flow
   (Fluent)** onto the empty *Project Schematic*. A column appears:
   Geometry, Mesh, Setup, Solution, Results.
3. **File → Save As** — save the project (e.g. `shield.wbpj`) in a folder
   whose path has no spaces or accented characters (Ansys sometimes
   stumbles on them).

### 7.2 Geometry

1. Right-click **Geometry → Import Geometry → Browse** → pick
   `fluid_domain.step`.
2. Double-click **Geometry** (SpaceClaim or Discovery opens) and check:
   - there is **one body** (a box with a shield-shaped cavity),
   - dimensions: measure a box edge (*Measure* tool) — 2000 mm, height
     1440 mm.
3. Close the geometry editor (Workbench keeps the changes).

> If the size is 1000× too small/large, the STEP was read in the wrong
> units: use *Scale* by 1000 or 0.001 in the geometry editor.

### 7.3 Mesh

1. Double-click **Mesh** — *Ansys Meshing* opens.

**a) Named Selections** — Fluent uses them to know which face is the inlet
and so on. At the top, switch the selection filter to *Face* (the cube icon
with a shaded face).

| Name | Which face |
|---|---|
| `inlet` | the box face at x = −1 m (upwind) |
| `outlet` | the box face at x = +1 m |
| `bottom` | the lower face (z = 0) |
| `top` | the upper face (z = 1.44 m) |
| `sides` | both side faces (y = ±1 m) — pick both with Ctrl |
| `shield` | **all** shield faces |

For each: click the face (Ctrl for more) → right-click → **Create Named
Selection** → type the name exactly as in the table (lower case).

The easiest way to pick the shield faces: **Ctrl+A** (selects every face) →
hold **Ctrl** and click the six box faces to remove them (rotate with the
middle mouse button) → right-click → Create Named Selection → `shield`.

**b) Mesh settings.** Click **Mesh** in the tree; in *Details* below set:

- *Physics Preference*: **CFD**, *Solver Preference*: **Fluent**
- *Element Size* (Defaults/Sizing): **80 mm**
- *Growth Rate*: **1.15**

**c) Fine mesh on the shield.** Right-click **Mesh → Insert → Sizing** →
*Geometry*: named selection `shield` (in Details, *Scoping Method* → Named
Selection) → *Element Size*: **4 mm**.

**d) Wall layers (inflation).** Right-click **Mesh → Insert → Inflation** →
*Geometry*: the whole body (pick it with the *Body* filter) → *Boundary*:
named selection `shield` → *Inflation Option*: **First Layer Thickness** →
*First Layer Height*: **0.5 mm** → *Maximum Layers*: **5** → *Growth Rate*:
**1.2**.

**e) Generate.** Right-click **Mesh → Generate Mesh**. Then click **Mesh**
and read *Elements* under Details → **Statistics**.

| Result | What to do |
|---|---|
| above the student limit (~1 million) | raise the shield *Element Size* to 5–6 mm, or cut the layers to 3 |
| below ~300 000 | you can refine (3 mm on the shield) — more accurate |
| generation error | see section 10 |

**f) Mesh quality.** Details → *Quality* → *Mesh Metric*: **Skewness** —
the maximum must be below 0.95 (ideally 0.9); **Orthogonal Quality** — the
minimum above 0.1. If not, try another shield element size.

Close Meshing. In Workbench right-click **Mesh → Update**.

### 7.4 Fluent — basic setup

Double-click **Setup**. In the *Fluent Launcher*:

- tick **Double Precision**,
- *Solver Processes*: the number of physical cores (the student licence may
  allow fewer — use what it allows),
- **Start**.

In the tree on the left (*Outline View*):

**General**
- *Solver Type*: **Pressure-Based**, *Time*: **Steady**.
- Tick **Gravity**: X = 0, Y = 0, **Z = −9.81** m/s².
- Click **Scale…** and check the domain extent: x −1 to 1, y −1 to 1,
  z 0 to 1.44 m. Then **Check** — no errors allowed.

**Models**
- **Energy**: On.
- **Viscous**: **k-epsilon (2 eqn)** → *Model*: **Standard** → *Near-Wall
  Treatment*: **Enhanced Wall Treatment** → OK.
- **Radiation**: **Discrete Ordinates (DO)**. In the same dialog:
  - *Angular Discretization*: Theta Divisions **4**, Phi Divisions **4**,
    Theta Pixels **3**, Phi Pixels **3**,
  - *Energy Iterations per Radiation Iteration*: **1**,
  - tick **Solar Load** → *Model*: **Solar Ray Tracing**,
  - *Sun Direction Vector*: turn off the *Solar Calculator* and enter
    **X = 0, Y = 0, Z = −1** (sun straight above),
  - *Illumination Parameters*: *Direct Solar Irradiation* = **1000 W/m²**
    (replaced by a parameter later, 7.9), *Diffuse Solar Irradiation* =
    **0**, leave *Spectral Fraction*.
  - OK.

> **Why two radiation methods?** White plastic absorbs only ~20 % of
> sunlight but ~90 % of thermal (long-wave) radiation. The gray DO model
> has one number for both. So the sun is handled by **Solar Ray Tracing**
> (with the solar absorptivity 0.2 and the shading between plates) and the
> thermal radiation of the ground, sky and shield by **DO** (emissivity 0.9).

**Materials → Fluid → air** (double-click):
- *Density*: **incompressible-ideal-gas** (density follows temperature →
  buoyancy).
- Leave the rest → **Change/Create**.

**Operating Conditions**: *Operating Pressure* 101325 Pa; *Operating
Temperature* = air temperature in K (298.15 K for 25 °C).

### 7.5 Fluent — boundary conditions

**Boundary Conditions** in the tree. Double-click each zone:

**`inlet`** — type **velocity-inlet**:
- *Momentum*: *Velocity Magnitude* **1 m/s** (a parameter later), normal
  to the boundary.
- *Turbulence*: *Intensity and Viscosity Ratio* — 5 % and 10.
- *Thermal*: *Temperature* **298.15 K**.
- *Radiation*: *External Black Body Temperature Method*: Boundary
  Temperature.

**`outlet`** — type **pressure-outlet**: *Gauge Pressure* **0 Pa**,
*Backflow Total Temperature* 298.15 K.

**`sides`** — type **symmetry**.

**`top`** — type **wall**:
- *Momentum*: **Specified Shear**, all components 0 (air slides along the
  ceiling without friction).
- *Thermal*: **Heat Flux** = 0.
- *Radiation*: *BC Type* **semi-transparent**, *Diffuse Irradiation* = the
  sky's long-wave (e.g. 370 W/m² at 25 °C; **0** to reproduce the
  specification's "sun only from above" — then set 0 in the program's *Sky
  long-wave* too).
- If the *Radiation* tab has **Participates in Solar Ray Tracing**, untick
  it (the ceiling must not shade the sun).

**`bottom`** — depends on the scenario:

| Scenario | Settings |
|---|---|
| **Ground** (300 W/m²) | type **wall**, *Momentum*: Specified Shear 0; *Thermal*: Heat Flux 0; *Radiation*: **semi-transparent**, *Diffuse Irradiation* = **300 W/m²** (a parameter later) |
| **Car roof** (70 °C) | type **wall**, *Momentum*: No Slip; *Thermal*: **Temperature** = **343 K**; *Internal Emissivity* **0.95**; *Radiation*: opaque |

**`shield`** — type **wall**:
- *Momentum*: No Slip.
- *Thermal*: **Heat Flux** = 0 (a thin plate gives the air what it absorbs).
- *Radiation*: *BC Type* **opaque**, *Internal Emissivity* **0.9**.
- *Solar Ray Tracing*: *Direct Visible*, *Direct IR* and *Diffuse
  Hemispherical* absorptivity **0.2** (the white plastic's solar
  absorptivity), transmissivity 0.

### 7.6 The thermometer and the output value

1. **Results → Surfaces → Create → Point…** → *Name*: `thermometer`,
   coordinates **x = 0, y = 0, z = 0.72** (or what the program gave in
   `README_FLUENT.md`) → Create.
2. **Solution → Report Definitions → New → Surface Report → Vertex
   Average**:
   - *Name*: `monitor_temperature`,
   - *Field Variable*: **Temperature → Static Temperature**,
   - *Surfaces*: `thermometer`,
   - tick **Report File**, **Report Plot** and **Create Output Parameter**
     (Workbench then gets an output parameter),
   - OK.

### 7.7 Solving and convergence

**Solution → Methods**:
- *Scheme*: **Coupled**, tick **Pseudo Time Method** (more stable).
- *Spatial Discretization*: everything **Second Order** / **Second Order
  Upwind** (including *Turbulent Kinetic Energy*, *Dissipation Rate*,
  *Energy* and *Discrete Ordinates*). First order is more robust but less
  accurate.

**Solution → Monitors → Residual**: *Absolute Criteria* — continuity,
x/y/z-velocity, k, epsilon: **1e-4**; energy **1e-6**; do **1e-6**.

**Solution → Monitors → Convergence Conditions**: add
`monitor_temperature`, *Stop Criterion* **1e-5**, *Previous Values to
Consider* **100** (the solve stops when the thermometer temperature stops
changing).

**Solution → Initialization**: **Hybrid Initialization** → Initialize.

**Run Calculation**: *Number of Iterations* **2000** → **Calculate**.

Two plots appear: the residuals (they should fall and level off) and
`monitor_temperature` (it should settle on a flat line).

### 7.8 Checking the setup is right (before running 15 points!)

This is **the most important section of part C**. One wrong entry and all 15
points are worthless. After solving the baseline point, check:

| Check | How | What you must see |
|---|---|---|
| **The sun shines from above** | Results → Contours → *Solar Heat Flux* (or *Radiation → Absorbed Radiation Flux*), surface `shield` | the **top plate** is lit (~0.2 × 1000 = 200 W/m²), the undersides and the inside are dark. If the undersides are lit, flip the *Sun Direction Vector* (Z = +1) |
| **Inlet air temperature** | Contours → Static Temperature, plane y = 0 | exactly 298.15 K at the inlet; warmed (or cooled) air only round and behind the shield |
| **Energy balance** | Reports → **Fluxes** → Total Heat Transfer Rate, all zones → Compute | the *Net* sum close to 0 (under ~1 % of the absorbed sun ≈ 0.2 × 1000 × top-plate area) |
| **Convergence** | residual and `monitor_temperature` plots | residuals below the criteria, temperature changing by under 0.005 K over the last ~200 iterations |
| **y+** | Contours → Turbulence → **Wall Yplus**, surface `shield` | mostly below 1–2, nowhere well above 5 (otherwise reduce *First Layer Height*) |
| **A sensible ΔT** | `monitor_temperature − 298.15` | hundreds of millikelvin to a few K; the same order as SU2 in part B |

### 7.9 Parameters and all 15 design points

**Creating the input parameters.** In Fluent, next to each value that should
vary, click the drop-down arrow and choose **New Input Parameter…**:

| Where | Parameter name |
|---|---|
| `inlet` → Velocity Magnitude | `wind_speed` |
| Solar Load → Direct Solar Irradiation | `solar_flux` |
| `bottom` → Diffuse Irradiation (ground), or → Temperature (roof) | `bottom_flux` (for the roof `roof_temperature`) |

> If a field has no *New Input Parameter* option, create a **Named
> Expression** (*Parameters & Customization → Expressions → New*), e.g.
> `solar_flux = 1000 [W/m^2]`, tick **Use as Input Parameter**, and type
> `solar_flux` into the field instead of the number (choose *expression* on
> the field).

Save (File → Save Project) and close Fluent. A **Parameter Set** block
appears in Workbench. Double-click it.

**Table of Design Points**:
1. Open `design_points.csv` in Excel.
2. Add rows to the Workbench table and copy the `wind_speed`, `solar_flux`,
   `bottom_flux` values from the CSV (Ctrl+C / Ctrl+V works; otherwise type
   them — it is 15 rows).
   - In the **roof scenario** copy the CSV's `bottom_temperature_k` column
     into `roof_temperature`.
3. At the top, **Update All Design Points**. Workbench solves point after
   point; expect hours. (*Retain* on a design point keeps each point's
   results for later viewing — more disk space.)

**Exporting the results:** right-click the table → **Export Table Data as
CSV** → save, e.g. `fluent_results.csv`.

> Name the parameters **exactly** `wind_speed`, `solar_flux`, `bottom_flux`
> and `monitor_temperature` — the program recognises them by these names on
> import (headers like `P1 - wind_speed [m s^-1]` are fine; the temperature
> may be in K or °C). In the roof scenario the export must also contain a
> `bottom_flux` column (add it in Excel from `design_points.csv`).

### 7.10 (Optional) DesignXplorer inside Ansys

To do the response surface and Monte Carlo in Ansys too:

1. From Toolbox → **Design Exploration** drag **Response Surface** under
   the Parameter Set.
2. **Design of Experiments**: *Design of Experiments Type*: **Central
   Composite Design**, *Design Type*: **Face-Centered** (matches the
   program's points) → *Preview* → *Update*.
3. **Response Surface**: *Response Surface Type*: **Genetic Aggregation**
   (or *Standard Response Surface – Full 2nd-Order Polynomials*) → Update.
   In *Goodness of Fit* check that the *Coefficient of Determination* is
   close to 1, and add *Verification Points*.
4. Drag in **Six Sigma Analysis** and set the input distributions (Uniform,
   with the program's bounds) → *Number of Samples* 10 000 → Update. The
   result is a histogram of `monitor_temperature` and probabilities.

Not required — the program does the same analysis (worst case and
reliability included) after importing (7.11).

### 7.11 Importing into the program

On the **Radiation shield study** sub-tab:

1. Set the form as Fluent was set (above all *Inlet air* and *Bottom*) — the
   program takes the inlet temperature from it.
2. **Import solved design points (CSV)…** → pick `fluent_results.csv`.
3. The program fits the response surface, runs the Monte Carlo and stores
   a new study labelled *imported*.

---

## 8. Part D — getting the most accurate data

A study is only as accurate as its **weakest link**. Work through this list:

### 8.1 Mesh independence (the most common error)

- Program: section 6.3 (coarse → medium → fine on DP0).
- Fluent: the same point on two or three meshes (e.g. 5 mm, 4 mm, 3 mm on
  the shield) as far as the cell limit allows.
- The mesh is adequate when ΔT changes by < ~0.03 K between the last two.
- The student limit does not allow an arbitrarily fine mesh — which is why
  SU2 in the program, with no limit, is valuable as a check.

### 8.2 Convergence

- Never use a result whose thermometer temperature is still changing at the
  end (7.7, 6.5).
- At **0.5 m/s** the flow can be unsteady (natural convection dominates). If
  `monitor_temperature` just oscillates round a value, take the **average of
  the last ~500 iterations**, or solve that point *Transient* and average.

### 8.3 Material properties — often more important than the mesh

- The shield's **solar absorptivity** (0.2 vs 0.3) changes ΔT more than
  most mesh settings. If you can, find it (paint/plastic data sheet,
  reflectance measurement) and also run the study with a worse value (aged,
  dusty surface).
- An emissivity of 0.9 is reliable for plastics and white paint.
- A shiny metal shield is very different (emissivity ~0.1) — enter it.

### 8.4 Radiation

- Fluent: *Theta/Phi Divisions* 4 × 4 is reasonable; try 6 × 6 once — if ΔT
  moves by more than ~0.02 K, keep 6 × 6.
- Program: 64 *Rays per facet* is enough; 256 as a check.
- Decide whether to include the sky's long-wave radiation (*Sky long-wave*
  auto, ~370 W/m²) — physically right; the specification leaves it out. It
  must be the same in the program and in Fluent.

### 8.5 Design points and the response surface

- Watch the **LOO error** (the *Surface error (LOO)* card). It should be
  well below the differences you care about (e.g. < 0.05 K when judging a
  0.5 K tolerance).
- If it is large: choose *Latin hypercube* with 25–30 points (*LHS
  points*), or the *Radial basis* surface.
- **Always re-solve the worst case directly** (as an extra design point
  with the *worst case* values) — the response surface is least accurate in
  the corners of the ranges.

### 8.6 Comparing the three methods

| Comparison | Meaning |
|---|---|
| SU2 and Fluent agree (± ~20 %) | a good sign — the result does not depend on the solver |
| Different sign or order of magnitude | a setup error; go through 7.8 and the matching inputs (sky, albedo) |
| The analytic model is well off | normal — it is only indicative; you can "calibrate" it with *Ventilation coefficient* to match CFD at the centre point, then use it to compare variants quickly |

### 8.7 Validation by measurement

A model cannot be more accurate than its inputs. The best evidence is a
measurement: the shielded thermometer next to an **aspirated reference
thermometer** (with a fan) on a sunny day, ideally on a car roof too.
Compare the measured error with the model's ΔT for the same conditions
(wind, sun).

### 8.8 What the model leaves out

- The thermometer as a body (a real sensor absorbs radiation itself) — the
  model reads the **air temperature** at a point.
- Unsteady effects (gusts, passing clouds).
- Low sun (the model has the sun straight overhead) — for morning or
  evening sun change the *Sun Direction Vector* in Fluent.
- In the program: radiation between the plates is grey and diffuse with
  ray-cast view factors (switchable, `plate_radiation`), and conduction
  within the plates is solved (see 6.1) unless switched off.

---

## 9. Reading the results

| Card / plot | Meaning |
|---|---|
| **Mean dT** | the average error over all random conditions |
| **95 % of \|dT\| below** | in 95 % of conditions the error is below this |
| **Worst \|dT\|** | the worst error found; the line under the cards gives its wind, sun and bottom radiation |
| **Reliability** | the % of conditions within tolerance (green ≥ 95 %, amber ≥ 80 %, red below) |
| **Surface error (LOO)** | the response surface's accuracy — see 8.5 |
| **Histogram** | the distribution of the error; the red lines are ± tolerance |
| **dT against wind** | three curves (weak/medium/strong radiation) from the response surface; dots are solved design points and must lie on or near the curves |
| **Sensitivity** | rank correlation; near ±1 = that input decides, near 0 = hardly matters |

**Example interpretation:** "Above 2 m/s of wind the error is below 0.2 K.
The worst case, +1.4 K, is at 0.5 m/s, 1150 W/m² and a hot roof.
Reliability 90 % for a 0.5 K tolerance. Bottom radiation matters most
(correlation +0.9) — on a car roof a low-emissivity bottom plate or more
clearance from the roof would help."

---

## 10. When something goes wrong

| Problem | Fix |
|---|---|
| Program: *meshing the air round the shield failed* | choose a finer *Mesh*; check the STEP is a solid without overlapping parts |
| Program: *the thermometer point … could not be read* | the thermometer point is inside a solid part of the shield (post, plate) — move it |
| Program: SU2 not found | Help → Check environment; run `Setup.bat` again |
| Fluent: mesh above the student limit | larger shield cells (5–6 mm), fewer inflation layers (3), *Growth Rate* 1.2 |
| Fluent: *Mesh generation failed* | check the geometry (small gaps, tiny edges); raise the shield *Element Size* or turn on *Capture Proximity* |
| Fluent: the solve diverges (residuals grow, *floating point* error) | start with *First Order*, switch to second order after ~300 iterations; check mesh quality |
| Fluent: residuals oscillate | light wind — see 8.2 (average, or transient) |
| Import: *lacks the column(s)* | rename the CSV columns to `wind_speed`, `solar_flux`, `bottom_flux`, `monitor_temperature` |
| ΔT absurdly large (tens of K) | wrong units (mm vs m), wrong sun direction, or absorptivity 0.9 instead of 0.2 — go through 7.8 |

---

## 11. Program vs Fluent: the differences

| | Program (SU2) | Ansys Fluent (as in this guide) |
|---|---|---|
| Turbulence model | SST k-ω (SU2 has no standard k-ε) | standard k-ε, Enhanced Wall Treatment |
| Sun | its own rays (shading between plates) | Solar Ray Tracing |
| Thermal radiation | rays: view of sky and ground, linearised emission | Discrete Ordinates |
| Plate-to-plate radiation | radiosity, grey diffuse (`plate_radiation`) | computed |
| Conduction in the plates | yes, conjugate FEM (`solid_conduction`) | no (heat flux 0), or yes with a solid zone |
| Mesh limit | none | ~1 million cells (Student) |
| Design points | automatic, overnight by script | Parameter Set → Update All Design Points |
| Response surface + Monte Carlo | in the program | in the program (after import) or DesignXplorer |
| Cost | free | free (Student), size-limited |

The two complement each other: Fluent has the fuller radiation, the program
has no mesh limit and solves points unattended. When they agree, you can
trust the result.

---

## See also

- [User Guide](USER_GUIDE.md), section 9 — every field of the shield study.
- [MCP and AI Guide](MCP_AI_GUIDE.md), the `radiation_shield_study` tool —
  the AI assistant can run the same study.
- [Tutorial](TUTORIAL.md) — installation and the basics.
