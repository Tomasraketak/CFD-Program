# AeroThermalStudio — User Guide

Reference for every setting: what it does, what it affects, and how to choose
a value. For a step-by-step introduction, read
[TUTORIAL.md](TUTORIAL.md) first.

**Contents**

1. [Conventions](#1-conventions)
2. [Geometry](#2-geometry)
3. [Farfield domain](#3-farfield-domain)
4. [Flight condition](#4-flight-condition)
5. [Fin hinge axis](#5-fin-hinge-axis)
6. [Mesh](#6-mesh)
7. [Solver](#7-solver)
8. [Results](#8-results)
9. [Sensor microclimate](#9-sensor-microclimate)
10. [Visualisation](#10-visualisation)
11. [Projects and settings](#11-projects-and-settings)
12. [AI assistant](#12-ai-assistant)
13. [Files on disk](#13-files-on-disk)
14. [Physics notes](#14-physics-notes)

---

## 1. Conventions

**Units.** Metres, seconds, kilograms, newtons, pascals, kelvin — except
where a field name says otherwise (`_deg` for degrees, `_c` for Celsius,
`_ms` for metres per second). Every field carries its unit in its name or its
tooltip.

**Coordinate system.** After alignment the body axis lies along **+X**, with
the flow arriving from −X. Your chosen reference origin sits at (0, 0, 0). All
results — forces, moments, centre of pressure, hinge positions — are in this
frame, not your CAD frame.

**Angles.** Angle of attack and sideslip are applied by the *solver*, by
tilting the incoming flow. The mesh is always built in the body frame. This is
why changing the angle does not require remeshing.

**Validation.** Every input is bounded. A value outside its range is rejected
with an explanation rather than silently clamped, in the GUI and over MCP
alike.

---

## 2. Geometry

### STEP file

Path to a `.step` or `.stp` CAD file. Drag and drop onto the window, or
browse.

The file must contain a **solid**, not a surface collection. If import reports
no solids, sew your surfaces into a solid in your CAD package first.

On import the geometry is healed: split faces are sewn, degenerate edges
removed, tiny faces repaired. If healing destroys the solid — which happens
with some simple shapes — the unhealed import is used instead and the report
says so.

### Nose direction

Which way the nose points **in your CAD file's coordinate system**.

| Option | When |
|---|---|
| `+X` … `-Z` | The body is aligned with a CAD axis |
| Custom vector | Anything else |

The custom vector is normalised automatically; only its direction matters.
A vector of zero length is rejected.

This is the single most consequential setting. Get it wrong and the body is
meshed sideways or backwards, and every result will be plausible and wrong.

### Reference origin

The point moved to the wind-tunnel origin. Use the **nose tip**.

This is the reference for the centre of pressure, so choosing something
meaningful makes results directly interpretable — "the centre of pressure is
0.62 m aft of the nose" rather than "0.62 m from some arbitrary point".

### Scale to metres

Multiplier converting CAD units to metres.

| CAD units | Value |
|---|---|
| Metres | `1.0` |
| Millimetres | `0.001` |
| Centimetres | `0.01` |
| Inches | `0.0254` |

**This is filled in for you.** When you import a STEP file, the unit it
declares is read out of the file and put in this box, and the line under the
file name says what was found — *"Sapphire.step, read as millimetres — 1.32 m
across"*. Check that sentence. If the size is not what you expect, the unit
is wrong and nothing downstream will notice.

The declaration is believed only as far as it is plausible. Some exporters,
OpenCASCADE among them, write a millimetre header onto a model whose
coordinates are plainly metres; following that blindly would shrink a 1 m
rocket to 1 mm. When the declared unit and the model's own size disagree,
the size wins and the note says so. When the file declares nothing at all,
the box says to check it.

A wrong value here scales the Reynolds number by the same factor and
invalidates everything. Check the reference length in the mesh report against
what you expect.

### Handing the file to the assistant

Importing a STEP file also tells the built-in assistant about it. You can
then write *"mesh the model I just imported, nose along +Y, coarse"* without
typing the path again, and the assistant will use the same file and the same
unit you see in the form. It confirms which file it used in its reply.

---

## 3. Farfield domain

The volume of air simulated around the body, sized in multiples of the body
length `L`.

| Setting | Default | Range | Notes |
|---|---|---|---|
| Upstream | 5 L | 1–50 | Nose to inlet |
| Downstream | 10 L | 1–50 | Base to outlet |
| Radial | 5 L | 1–50 | Axis to outer boundary |
| Shape | cylinder | cylinder / box | |

**Choosing values.** The defaults suit most external aerodynamics.

- *Too small* and the boundary interferes with the solution — the classic
  symptom is a result that changes when you enlarge the domain.
- *Too large* and cells are wasted on air that does nothing.

Downstream is larger because the wake takes distance to develop. At supersonic
speeds you can often reduce the upstream extent, because disturbances cannot
travel forward ahead of the bow shock. At transonic speeds do the opposite:
disturbances propagate a long way, so give it more room.

**Shape.** `cylinder` wastes fewer cells around a slender body. `box` suits
enclosures and ground-proximity cases such as the sensor housing.

---

## 4. Flight condition

| Setting | Range | Notes |
|---|---|---|
| Speed as | mach / tas | How the speed value is interpreted |
| Speed | Mach 0.05–3.5, or m/s | |
| Angle of attack | ±20° | Pitch relative to the flow |
| Sideslip | ±20° | Yaw relative to the flow |
| Altitude | −610 to 32 000 m | Sets pressure, temperature, density |

**Mach or true airspeed.** Mach number is what governs the physics —
compressibility, shocks, the choice of numerical scheme. True airspeed is what
a flight computer reports. The two are related through the speed of sound,
which depends on temperature and therefore altitude, so specifying 200 m/s at
sea level and at 10 km gives different Mach numbers.

**The ±20° limit** is not arbitrary. Beyond it, flow over a slender body
separates massively and a steady RANS solution stops being meaningful. The
answer would still be a number; it would not be right.

**Altitude** uses the ISA-1976 standard atmosphere, validated against the
published tables. Alternatively specify pressure and temperature directly
(available in project files and over MCP) when you have measured conditions.

The derived state is displayed live:

```
M 2.000  |  V 680.6 m/s  |  p 101.3 kPa  |  T 288.2 K
```

---

## 5. Fin hinge axis

| Setting | Meaning |
|---|---|
| Name | Label used in results and curves |
| Point | Any point on the hinge line, metres, in the aligned frame |
| Direction | The rotation axis |

**How torque is computed.** The solver reports the aerodynamic moment about a
reference point. That is not what a servo feels. The program:

1. Takes the total force and the moment about the reference point;
2. Transfers the moment to your hinge point, using the parallel-axis
   correction `M_hinge = M_ref + (r_ref − r_hinge) × F`;
3. Projects the result onto your hinge direction.

The output is a scalar in newton-metres. Only the component along the axis can
rotate the fin; the rest is carried by the bearing.

**Sign.** Positive follows the right-hand rule about the direction you gave.
Reversing the direction reverses the sign. Magnitude is what sizes the servo.

**Sanity check.** A force acting exactly through the hinge line gives exactly
zero torque, regardless of where the solver's reference sits. This is asserted
by the test suite.

---

## 6. Mesh

### Resolution

| Preset | Aerodynamic cells | Thermal cells |
|---|---|---|
| coarse | 250 000 – 400 000 | 150 000 – 230 000 |
| medium | 400 000 – 600 000 | 230 000 – 320 000 |
| fine | 600 000 – 750 000 | 320 000 – 400 000 |

The mesher does not simply apply a preset size. It meshes, counts, and adjusts
the characteristic size until the count lands inside the band — up to four
attempts. That is what makes the band a guarantee rather than a hope on
arbitrary geometry.

The bands are chosen so a solve takes 3–8 minutes on a 6-core machine with
16 GB of RAM.

### Prism layers

Range 5–8, default 7. Thin, flat cells stacked against the wall to resolve the
boundary layer.

More layers resolve the near-wall profile better and cost cells. Seven is a
good balance for wall-function turbulence modelling.

### Target y⁺

Range 30–300, default 45.

y⁺ is a dimensionless wall distance. It tells you where the first cell centre
sits within the boundary layer:

| y⁺ | Region | Suitable for |
|---|---|---|
| < 5 | Viscous sublayer | Wall-resolved models, very expensive |
| 30–300 | Log layer | **Wall functions** — what this program uses |
| between | Buffer layer | Avoid; neither approach is valid |

Stay in 30–60 unless you know why you want otherwise. The mesher computes the
required first-layer height from the flow condition, so you set the physics
target and it works out the geometry.

The achieved y⁺ is reported after meshing, and it should match what you asked
for exactly.

### Minimum quality

Reported after meshing: the worst cell as a fraction of a perfectly-shaped
one.

| Value | Assessment |
|---|---|
| > 0.3 | Healthy |
| 0.1 – 0.3 | Acceptable |
| < 0.05 | Expect convergence trouble |

---

## 7. Solver

| Setting | Default | Notes |
|---|---|---|
| Turbulence model | SST | SST k-ω, or SA (Spalart–Allmaras) |
| Convective scheme | automatic | JST below Mach 0.8, Roe above |
| CFL number | 5.0 | Larger converges faster but less robustly |
| Max iterations | 5000 | Hard cap |
| Convergence residual | −5.0 | log₁₀ of RMS density residual |
| MPI ranks | 10 | Two below the thread count |

### Scheme selection

Chosen automatically from the Mach number:

- **Below Mach 0.8** — JST central differencing with scalar dissipation.
  Efficient and accurate in smooth subsonic flow.
- **At and above Mach 0.8** — Roe upwind with second-order MUSCL
  reconstruction and the Venkatakrishnan limiter, plus an entropy fix. The
  limiter is what prevents oscillations at shocks; the entropy fix stops the
  Roe solver admitting unphysical expansion shocks.

Override it only if you know why.

### Convergence

The run stops when **either**:

- the density residual reaches the threshold, **or**
- the drag coefficient has been steady across a trailing window.

The second matters in practice: a RANS solve often reaches usable forces well
before the residual target, and the forces are what you came for.

### MPI ranks

How many processes the solver splits across. Use two fewer than your logical
processor count so the interface stays responsive — 10 on a 12-thread CPU.

More ranks than physical cores gives diminishing returns; each rank also needs
memory, and too many on a large mesh will exhaust 16 GB.

---

## 8. Results

| Quantity | Symbol | Meaning |
|---|---|---|
| Drag coefficient | C_d | Drag / (q·S) |
| Lift coefficient | C_l | Lift / (q·S) |
| Side-force coefficient | C_s | Side force / (q·S) |
| Pitching moment | C_m | Moment / (q·S·L) |
| Forces | F_x, F_y, F_z | Body-axis components, newtons |
| Drag / lift / side force | | Wind-axis, newtons |
| Centre of pressure | | Axial station, metres |
| Hinge torque | τ | Newton-metres about each hinge |

Here `q = ½ρV²` is dynamic pressure, `S` the reference area (body
cross-section unless overridden) and `L` the reference length (body diameter
unless overridden).

**Body axes versus wind axes.** Body axes are fixed to the rocket. Wind axes
are aligned with the airflow. At zero angle of attack they coincide; at angle
they differ by exactly that angle. Drag and lift are wind-axis quantities by
definition.

**Centre of pressure** reports `NaN` when there is no transverse force — at
zero incidence on a symmetric body there is genuinely no defined centre of
pressure, and reporting a number would invite you to trust something
meaningless. Run at 2–5° to get a usable value.

---

## 9. Sensor microclimate

### Environment

| Setting | Default | Range | Notes |
|---|---|---|---|
| Vehicle speed | 2.0 m/s | 0–50 | Drives ram air through the intake |
| Ambient air | 25 °C | −50 to 70 | True air temperature — the reference |
| Solar flux | 800 W/m² | 0–1400 | ~1000 is clear noon; 0 is night |
| Height above roof | 0.1 m | 0.01–2 | Compared against the roof's thermal layer |

### Surface properties

| Setting | Default | Notes |
|---|---|---|
| Housing solar absorptivity | 0.30 | Fraction of sunlight absorbed. White ~0.1, black ~0.95 |
| Roof solar absorptivity | 0.65 | Painted metal. Bare aluminium ~0.2 |
| Housing emissivity | 0.90 | Long-wave emission. Most non-metals are 0.85–0.95 |
| Housing conductivity | 0.18 W/(m·K) | PLA/PETG. Aluminium is ~200 |
| Sensor position | — | BMP580 die coordinate inside the enclosure |

Absorptivity and emissivity are separate properties at different wavelengths.
A white paint can have absorptivity 0.2 in sunlight and emissivity 0.9 in the
infrared — which is exactly why white paint stays cool.

### Outputs

| Readout | Meaning |
|---|---|
| Sensor reads | What the BMP580 would report |
| True ambient | The real air temperature |
| **Measurement error** | The difference — the number you care about |
| Roof surface | How hot the roof gets |
| Housing | Housing temperature |
| Intake flow | Ram-air mass flow, mg/s |
| Roof thermal layer | Thickness of the roof's warmed air layer, mm |

The error is colour-coded: green below 0.5 K, amber below 2 K, red above.

**The thermal layer number is the important one.** If your intake height is
below it, you are sampling air the roof has already warmed and no amount of
housing design will fix it. The panel states which regime you are in.

### Two levels of fidelity

**Analytical** (instant, default in the GUI). Solves the coupled roof,
housing, tube-flow and chamber energy balances. Fast enough to update as you
drag a slider, which makes it a design tool rather than a batch job.

**Conjugate CFD** (minutes, needs a thermal mesh). Full 3D solution with heat
conducted through the solid housing and convected by the internal air. Resolves
detail the analytical model averages over.

The analytical model also seeds the CFD run's radiation linearisation, so the
two work together rather than being alternatives.

---

## 10. Visualisation

### Modes

| Mode | Shows |
|---|---|
| `surface_pressure` | C_p or absolute pressure on the body |
| `mach_slice` | Mach number on a cutting plane |
| `streamlines` | Flow paths, coloured by velocity or temperature |
| `thermal` | Temperature contours with the sensor marked |

### Colormaps

`turbo`, `coolwarm`, `viridis`, `jet`, `plasma`, `inferno`.

Use **coolwarm** for temperature — it is diverging, so it reads naturally
about a midpoint. Use **viridis** for anything that will be printed in
greyscale or viewed by a colour-blind reader; it is perceptually uniform.
**turbo** gives the most visual contrast for pressure and Mach fields.

### Camera views

`isometric`, `front`, `back`, `side`, `top`, `bottom`, `nose_quarter`,
`tail_quarter`.

### Resolutions

`preview` (960×540), `hd` (1920×1080), `2k` (2560×1440), `4k` (3840×2160).

Use `preview` while exploring and `4k` for figures.

### Export

`.vtk` / `.vtu` for analysis in ParaView, `.gltf` for an interactive 3D scene
that opens in a browser.

---

## 11. Projects and settings

### Projects (`.atsproj`)

A project stores the complete setup: geometry and orientation, domain, mesh
settings, flight condition, solver settings, hinge axes, sensor scenario,
sweep definition, notes, and the last mesh used.

Plain JSON — readable, diffable, and worth committing to version control
alongside your CAD.

| Action | Shortcut |
|---|---|
| New | Ctrl+N |
| Open | Ctrl+O |
| Save | Ctrl+S |
| Save as | Ctrl+Shift+S |

A project referencing a mesh that has since been deleted reports that on load
and disables Run, rather than failing later inside the solver.

A project written by a newer version of the program is **refused**, not
partially read. Silently dropping fields an older build does not understand
would lose settings without telling anyone.

### Preferences

**Settings → Preferences:**

| Setting | Effect |
|---|---|
| Default MPI ranks | Starting value for new runs |
| Default colormap | Preselected in the viewport and renders |
| Render resolution | Preselected for saved images |
| Mesh resolution | Starting preset for new projects |
| Ask before discarding | Confirm before New/Open loses unsaved work |
| Autosave before solve | Save the project when a solve starts |
| Live sensor preview | Re-solve as sliders move |
| Warn about environment | Flag a missing SU2 or MPI at startup |

A corrupted settings file falls back to defaults rather than preventing
startup.

---

## 12. AI assistant

The **AI Assistant** tab lets you say what you want in ordinary language and
have the program do it. The assistant reaches the platform through exactly the
same thirteen tools the MCP server exposes — it can do what you can do through
the interface, and nothing else.

It runs on a model of your choice through [OpenRouter](https://openrouter.ai),
so you need an OpenRouter account and an API key. The program itself contains
no model and sends nothing anywhere until you set a key.

### Setting the API key

1. Get a key at [openrouter.ai/keys](https://openrouter.ai/keys).
2. Paste it into **Key** on the left of the AI Assistant tab and press
   **Save key**.

The box is cleared the moment the key is stored, and it is never shown again
— only a masked form such as `sk-or-...a1b2`, plus a statement of where it is
kept.

Where it is kept depends on the machine:

| Backend | When it is used | Secure |
|---|---|---|
| `OPENROUTER_API_KEY` environment variable | Always wins if set | Yes — nothing is written to disk |
| Windows Credential Manager | Whenever the `keyring` package is installed | Yes — protected by your login |
| `credentials.json` | Only if no credential store is available | **No** — plain text, owner-only permissions |

The last case is announced with a dialog that says so plainly. It is a
fallback, not a security measure: it protects the key from other accounts on
the machine and from nothing else. `Setup.bat` installs `keyring` so that the
Credential Manager is used instead.

**The key never enters a project file or `settings.json`.** Those are plain
JSON you are meant to commit to version control; a key there would end up in a
repository. **Remove** deletes the key from every backend at once.

### Choosing a model

Any OpenRouter model id works. The dropdown starts with a handful of
suggestions; **Fetch available models** replaces them with the live catalogue
your account can actually reach, because the catalogue changes constantly and
a list hard-coded here would go stale.

The default is `deepseek/deepseek-chat` — cheap, fast and good enough for this
job. A model must support tool calling, or the assistant can only talk. The
choice is remembered between sessions.

**You pay OpenRouter for what the assistant uses**, per token, at that model's
rate. A short question costs a fraction of a cent; a long session of tool
calls costs more. The token count for each request appears in the status bar.

### Talking to it

Type the request and press **Send**, or Ctrl+Enter. Answers come in whatever
language you write in. Useful requests look like:

- *"Mesh C:\models\rocket.step with the nose along +X at coarse resolution."*
- *"How far off will the BMP580 read at 900 W/m² and 3 m/s?"*
- *"Compare the sensor error for a white and a black housing."*
- *"Sweep Mach 0.5 to 3 at 5 degrees and tell me the worst-case hinge torque."*
- *"Check the environment and tell me whether I can run a solve."*

Every tool call appears in the transcript as it happens, with its arguments
and how long it took. An assistant that silently started a half-hour sweep
would be worse than no assistant at all.

**New conversation** forgets the history. Do that when you switch topic: the
whole conversation is sent with every request, so a long one costs more and
gives the model more chance to confuse two studies.

### Staying in control

| Control | What it does |
|---|---|
| Ask before meshing, solving or sweeping | Confirms each long-running step, showing the tool, its arguments and the expected runtime. On by default. |
| Max tool rounds | Caps how many tool-calling rounds one request may take. Stops a confused model spending your credit in a loop. Default 12. |

Declining a step tells the model you declined and asks what you would prefer,
rather than having it retry the same thing.

### What it cannot do

- **No shell commands, no arbitrary file access, no code execution.** The tool
  list is the boundary, and the tools take validated parameters only.
- **No invented numbers.** It is instructed to report what the tools returned,
  including when a solve did not converge or a cell count missed its band.
  Check the transcript against the Results panel if a figure matters.
- **It is not a CFD engineer.** It can drive the program competently; it
  cannot tell you that your geometry is wrong. Judgement stays with you.

Requests you type and the simulation parameters involved are sent to
OpenRouter and to the model provider you choose. Results and mesh files stay
on your machine — only the numbers in the tool results travel.

### When something goes wrong

| Message | Meaning |
|---|---|
| *No OpenRouter API key is set* | Save a key, or set `OPENROUTER_API_KEY` |
| *OpenRouter rejected the API key* | The key is wrong or was revoked — check it at openrouter.ai/keys |
| *insufficient credit* | Add credit, or pick a cheaper model |
| *rate-limiting this key* | Wait a moment; free-tier models are throttled |
| *could not reach OpenRouter* | Network, proxy or firewall |
| *I stopped after N rounds* | Break the request into smaller steps |

---

## 13. Files on disk

Default location `%LOCALAPPDATA%\AeroThermalStudio`, overridable with the
`ATS_DATA_ROOT` environment variable.

```
AeroThermalStudio/
├── runs/                      one folder per mesh, simulation and sweep
│   ├── mesh-YYYYMMDD-HHMMSS-xxxxxx/
│   │   ├── record.json        metadata
│   │   ├── mesh.su2           the mesh
│   │   ├── mesh_request.json  what was asked for
│   │   └── mesh_result.json   what came out
│   ├── aero-.../
│   │   ├── solver.cfg         the SU2 configuration
│   │   ├── history.csv        convergence history
│   │   ├── forces_breakdown.dat
│   │   ├── flow.vtu           volume solution
│   │   ├── result.json        the reduced result
│   │   └── renders/           generated images
│   └── sweep-.../
│       ├── point_000/ …       one folder per point
│       └── sweep.json         curves and peaks
├── projects/                  saved .atsproj files
├── samples/                   generated sample CAD
├── su2/                       solver binaries, if installed here
└── settings.json
```

Volume solutions are large. Delete old run folders to reclaim space — the
program reads the registry fresh each time and simply will not list what is
gone.

---

## 14. Physics notes

### Why y⁺ matters so much

Near a wall, velocity changes extremely quickly. Resolving that directly needs
cells so thin that a realistic model would run to millions of cells and hours
of solve time.

Wall functions model the near-wall profile analytically instead of resolving
it, which lets the first cell be much larger — but only if it lands in the
log-law region, y⁺ between 30 and 300. Too close and the assumption is wrong;
too far and the log layer is not resolved.

This is why the mesher works backwards: you give it a y⁺ target and the flow
condition, and it computes the cell height.

### Why prism layers, not tetrahedra

Boundary-layer cells must be thin perpendicular to the wall but can be much
larger along it — aspect ratios of 1000:1 are normal. Tetrahedra cannot do
this well; stretched tetrahedra have terrible numerical properties.

Prisms can: a triangle stretched along the wall normal. This program extrudes
them with its own advancing-layer algorithm, because Gmsh's 3D boundary-layer
extrusion does not work reliably. See the README for the details.

Without prisms, y⁺ 45 on a 1 m body would need millions of surface triangles
alone — roughly five times the cell ceiling that keeps solves inside the
target runtime.

### Why shocks need a different numerical scheme

A shock is a discontinuity: pressure, density and temperature jump across a
distance of a few molecular mean free paths.

Central differencing assumes the solution is smooth. At a discontinuity it
produces oscillations that grow until the solve fails.

Upwind schemes respect the direction information travels and capture shocks
cleanly, at the cost of more numerical dissipation in smooth regions. The
limiter reduces the scheme to first order near the shock — where robustness
matters — while keeping second order elsewhere.

Hence the automatic switch at Mach 0.8.

### Why the sensor reads high

Three mechanisms, in order of importance for the default tram case:

1. **Housing heating.** The sun heats the housing; sampled air warms as it
   passes through the chamber. Dominant.
2. **Intake position.** If the intake sits inside the roof's thermal boundary
   layer, it samples pre-warmed air. Negligible at 100 mm; dominant at 10 mm.
3. **Self-heating.** The BMP580's own 1 mW. About 5 mK — a thousandth of the
   total.

At night the sign flips: with no sun, the housing radiates to the cold sky and
the reading falls *below* ambient.

---

## See also

- **[TUTORIAL.md](TUTORIAL.md)** — step-by-step introduction
- **[MCP_AI_GUIDE.md](MCP_AI_GUIDE.md)** — AI agent interface
- **[../../README.md](../../README.md)** — architecture and implementation
