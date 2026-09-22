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

**Coordinate system — rocket axes.** Whichever way the CAD was drawn, the
rocket is shown and reported **standing up, nose along +Z**, the way it sits
on the pad and flies. Your chosen reference origin (normally the nose tip)
sits at (0, 0, 0), so the body occupies negative Z: a fin near the tail of a
1.3 m rocket is at about z = −1.2. The viewport, rendered images, force
components, centre of pressure and fin hinge positions all use these axes.

| Rocket axis | Points | Force along it |
|---|---|---|
| +Z | Along the nose (up, in vertical flight) | Drag of a rocket flying nose-first is **negative** F_z |
| +X | The pitch direction | Lift from a positive angle of attack is **+F_x** |
| +Y | Completes a right-handed set | Side force from sideslip |

A model drawn with its nose already on +Z keeps its own axes unchanged.

**The solver frame.** Internally the mesh lies with the body along **+X** and
the flow arriving along +X, because that is what SU2's angle-of-attack
convention assumes. The two frames differ by a fixed rotation
(x_rocket = z_solver, y_rocket = y_solver, z_rocket = −x_solver), so nothing
is re-meshed to show you the rocket upright. Solver-frame numbers are still in
`result.json` and the MCP replies, labelled as such.

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

### Nose points to

The direction the **nose points** in your CAD file. A rocket drawn standing
up, tip at the top of a Y-up model, is **`+Y`**.

| Option | When |
|---|---|
| `+X` … `-Z` | The body is aligned with a CAD axis |
| Custom vector | Anything else — the direction the nose points |

The custom vector is normalised automatically; only its direction matters.
A vector of zero length is rejected.

Whatever you choose here, the model is then shown nose-up along +Z.

> The meshing parameter underneath, `nose_direction`, is the opposite: the
> nose-to-*tail* direction, so a nose at +Y is `nose_direction = -Y`. The
> buttons used to show that value directly, which lit up "−Y" beside a note
> saying "nose at +Y". They now show where the nose is, and the conversion
> happens out of sight. Over MCP and in project files the parameter keeps its
> original meaning.

**This is filled in for you too.** On import the program finds the longest
axis of the model and looks for fins: the end that carries them is the tail.
Only when neither end has fins does it compare how thick each end is, since
a nose tapers and a tail does not. Fins come first because thickness alone
can be fooled by a motor nozzle, which is thin and sits at the very end of
the tail. The buttons are set from that and the note under the file name says which end it found —
*"nose at +Y"*.

When both ends are alike — a plain tube, a body with a boat tail — it will
not guess. The note asks which end the nose is on, and the assistant asks the
same question rather than picking one.

This is the single most consequential setting. Get it wrong and the body is
meshed sideways or backwards, and every result will be plausible and wrong:
a rocket flying tail-first still produces a complete drag polar.

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

### Seeing what you imported

The model appears in the 3D viewport as soon as it is read, **standing up
with the nose along +Z**, seen from the side, with an axis triad in the
corner. It is tessellated coarsely — a few seconds, no boundary layers, no
farfield — purely so you can look at it.

**Look at it.** This is not decoration: the model goes through exactly the
alignment the mesh will, and is then stood on its tail. The nose belongs at
the top. If it is at the bottom, the nose direction is wrong and every result
that follows would be a perfectly plausible set of forces for a rocket flying
tail-first.

If the preview cannot be built, the log says so and the import carries on: a
body that will not tessellate coarsely may still mesh properly with the real
settings.

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
| Point | Any point on the hinge line, metres, in rocket axes (nose along +Z) |
| Direction | The rotation axis, in rocket axes |

The hinge line is drawn into the viewport over the upright model, so you can
see it sits where the fin does. Projects saved before rocket axes existed hold
their hinges in the solver frame; they are converted when the project is
opened and mean exactly the same hinge.

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

The minimum is set by the single worst prism, and that is nearly always in a
concave corner — a nozzle meeting a flat base, a fin root — where the layers
are squeezed whatever the rest of the mesh looks like. Read it together with
the **poor prism count** (quality below 0.3): a handful out of several
hundred thousand is a local corner, not a bad mesh, and not a reason to
throw the mesh away.

Away from the wall, the tetrahedra grow in proportion to the distance from
the body, at a rate set by the resolution (coarse fastest, fine slowest).
Without that, cells a hand's width off a 75 mm rocket's nose were larger
than the rocket and no shock wave could survive on them.

---

## 7. Solver

| Setting | Default | Notes |
|---|---|---|
| Turbulence model | SST | SST k-ω, or SA (Spalart–Allmaras) |
| Scheme | Auto | JST below Mach 0.8, Roe above; or pick JST, ROE, AUSM, HLLC |
| CFL start | Auto | 5.0 below Mach 0.6, 2.0 up to Mach 1.2, 1.0 above. A number you type is always used as given |
| CFL growth | Auto | Factor the adaptive CFL grows by each iteration: 1.15 / 1.10 / 1.05 by regime |
| CFL max | Auto | Where the adaptive CFL stops: 100 / 50 / 25 by regime |
| Max iterations | 5000 | Hard cap |
| Convergence residual | −5.0 | log₁₀ of RMS density residual |
| MPI ranks | 10 | Two below the thread count |
| Rescue on divergence | on | Retry a blown-up solve at first order, then restart at second |
| Stall timeout | 300 s | Give up if the solver goes silent for this long |
| Wall-time limit | 7200 s | Give up after this long in total |

**The start is only the start.** The adaptive CFL grows from the start
value every iteration. Subsonic runs used to double it, which took CFL 5 to
100 in five iterations and made a Mach 0.7 case diverge on every mesh it was
given; typing a low CFL did not help, because the ramp took it straight back
up. To run cautiously, lower **CFL max** and keep **CFL growth** near 1.05.
From Mach 0.6 a run is also started as gently as a transonic one: on a finned
body the flow over the nose shoulder and the fin leading edges reaches sonic
speed locally around Mach 0.7.

### When a solve blows up

A supersonic cold start is the fragile case. The first iterations reconstruct
across a shock that does not exist yet, on a mesh sized for the converged
solution, and if the CFL number is climbing fast at the same time the solve
can reach a negative pressure and die with `SU2 has diverged (NaN detected)`
within a handful of iterations.

Two things now happen automatically:

- **The blow-up is caught immediately.** The run stops at the iteration that
  went non-finite rather than grinding on to the iteration cap.
- **It is retried.** The solve runs again as **first-order Roe** at a low CFL
  (0.5, growing 1.05× per iteration to at most 10) to establish the flow in
  roughly the right shape, then restarts from that solution at second order
  in the scheme originally chosen. The result is marked **rescued** and carries a
  note saying so. The numbers are valid, but the case needed help to get
  there, and that usually means the mesh is coarser than the flow condition
  wants. If it happens often, refine the mesh rather than relying on the
  rescue.

If the rescue diverges too, the failure is reported with both attempts and
their settings. A first-order scheme at that CFL rarely fails on numerics
alone, so look at the mesh quality first — a minimum below about 0.2 is
suspect — and then try a smaller CFL max.

### A solve that never returns

A solver run now has two deadlines, both settable above. **Stall** catches the
worst case: an aborted MPI job can leave a rank alive holding the output pipe,
and the program used to wait on that read forever — no output, no solver
working, no end. Whichever deadline is reached, the whole process tree is
killed and the run fails with a reason rather than sitting silent. A solve
started by the assistant can also be ended with its **Stop** button.

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
| Forces | F_x, F_y, F_z | Components along the **rocket axes**, newtons: F_z along the nose |
| Drag / lift / side force | | Wind-axis, newtons |
| Centre of pressure | | Z station in rocket axes, metres (negative: behind the origin) |
| Hinge torque | τ | Newton-metres about each hinge |

Here `q = ½ρV²` is dynamic pressure, `S` the reference area (body
cross-section unless overridden) and `L` the reference length (body diameter
unless overridden).

**The force along each axis of the rocket** is the second row of cards —
*Force along rocket X / Y / Z*. For a rocket flying straight up at zero angle
of attack, F_z is the drag with a minus sign (it pushes the rocket back
towards its tail) and F_x, F_y are what is left of numerical noise. At an
angle of attack F_x grows: that is the normal force that turns the rocket.

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
| `mach_slice` | Mach number on a cutting plane, with iso-Mach lines |
| `schlieren` | Density gradient on a cutting plane, like a wind-tunnel schlieren photograph |

**Why schlieren for shocks.** A slender ogive nose at Mach 1.3 makes a weak
oblique shock: the Mach number drops by a few hundredths across it, which a
colour map spread from the stagnation point to freestream barely shows. The
density gradient jumps by orders of magnitude across any shock, however weak,
so the Mach cone off the nose and the one off the fins stand out plainly.
The Mach slice's colour range is taken from the picture itself (2nd–98th
percentile by area), not its extremes, and iso-Mach lines bunch up where a
shock is.
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

Rocket results are drawn **nose-up along +Z**. `front` looks at the nose
from ahead of it, `side` looks straight at the pitch plane — the plane the
default Mach slice lies in, so it is the view to use for a shock picture.

A Mach slice is cropped to the rocket plus three quarters of a body length
around it. The farfield is five to ten lengths away, and a slice through all
of it would show the rocket as a speck.

### Graphics tab

Every image lands in the **Graphics** tab, whoever drew it: the tab itself,
the assistant, or an external MCP client using the same data folder. It lists
them newest first with thumbnails and shows the selected one large.

| Button | Does |
|---|---|
| **Export …** | Save a copy anywhere, as PNG (unchanged) or JPEG |
| **Export all from this run …** | Copy every image of that run into a folder |
| **Copy** | Put the image on the clipboard, to paste into a report |
| **Open** | Open it in the system image viewer (also: double-click) |
| **Show folder** | Open the folder the image is stored in |

The row at the top draws a new image: pick a finished run, the image type,
view, colours and size, then **Render**. When a solve finishes in the
Aerodynamics tab, a Mach slice from the side and the surface pressure are
drawn automatically.

Images the assistant renders also appear inline in its conversation, with a
link straight to the Graphics tab.

The files themselves are in `runs/<sim_id>/renders/` under the data folder.

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

The **Model** drop-down holds a shortlist: `deepseek/deepseek-v4.1-flash`,
`meta/muse-spark-1.3-contributor`, `qwen/qwen3.7-flash`,
`openai/gpt-5.6-luna` and `deepseek/deepseek-chat`. Its first entry,
**Custom…**, opens a box underneath where you can type any id at all.
**Fetch available models** adds the live catalogue your account can actually
reach below the shortlist, and offers it as suggestions while you type a
custom id — worth doing, because OpenRouter's catalogue changes weekly and an
id that is not on it is rejected at the first request.

A model must support tool calling, or the assistant can only talk. The choice
is remembered between sessions.

**You pay OpenRouter for what the assistant uses**, per token, at that model's
rate. A short question costs a fraction of a cent; a long session of tool
calls costs more.

### Picking a conversation up again

Every conversation is saved on this computer when the assistant answers, and
again when the program closes. The **Conversation** list above the chat holds
them newest first. Pick one and the transcript comes back, the assistant
remembers everything that was said and every tool result — so "run the same
mesh at Mach 0.9" works — and a note lists the meshes, simulations and
projects the conversation produced. Click one to open it: a simulation
brings its results back into the Aerodynamics tab and makes its mesh the
current one, a project opens as a project. **Delete** removes the selected
saved conversation. Conversations are kept in `ai_chats` under the data
folder; they never contain the API key.

### Seeing the time and the solve

The line under the chat says what is happening and for how long, twice: how
long the current step has been running, and the whole request — *"Running
run_aerodynamic_simulation — step 2:41 · total 4:05"*. The Aerodynamics tab
does the same beside its progress bar.

When a solve is running, whoever started it, a live convergence chart shows
the density residual and the force coefficients, with a line saying the
iteration, the speed in iterations per second, and whether the residual is
falling (*converging*), flat (*stalled*) or rising (*diverging*).

Results the assistant gives as a table are drawn as a table.

### Watching what it costs

While a request runs, the line under the transcript shows what it has spent
so far:

```
4,812 tokens · 61.3 tok/s · $0.0038 · 14s
```

- **tokens** — prompt and completion together, for this request
- **tok/s** — generation rate, measured against time spent waiting on the
  model, not against wall time. A request that spends four minutes meshing
  has not slowed the model down, and this number should not pretend it has.
- **$** — OpenRouter's own figure for the request, not an estimate from a
  price list. Some providers do not report one; the field is then left blank
  rather than showing a zero you might believe.
- **seconds** — wall time, which keeps moving between rounds so you can tell
  a slow request from a stuck one.

The figures update after each round of the conversation, so a request that is
going to be expensive becomes visible while there is still time to stop it.
**New conversation** resets the meter.

### Following along while it works

The assistant writes what it is doing into the transcript **as it happens**,
not when it finishes:

```
— round 1 —
I will check the toolchain before meshing anything.
▸ get_active_geometry()
  ok — 0.0 s
▸ set_geometry_and_mesh(mesh_resolution=coarse, sizing_mach=1.3)
  ok — 101.6 s
— round 2 —
```

Four things are on that page that were not before. The **round markers** show
how many times the model has been round the loop. The *italic line* is the
model's own account of what it is about to do — it was always being sent and
always being thrown away. Each **tool call** appears with its arguments before
it runs, so a four-minute mesh tells you which file and which settings it is
working on. And each **outcome** carries the time it took.

The one-line status under the transcript still shows the current step, and the
meter beside it the running cost.

**A dropped connection no longer ends the session.** If the network fails, or
OpenRouter is briefly overloaded or rate-limiting, the request is tried again
— up to three times, with a widening gap — and each retry is written into the
transcript so it does not look like a hang. A rejected key or an empty balance
is *not* retried: those will not improve by asking again.

There is an honest cost to this. A completion is not idempotent for billing:
if the connection drops after the model has already generated its answer,
retrying pays for that answer twice. Losing a seventeen-minute session costs
more, so retrying is on by default — set `ai_retry_attempts` to `1` in
`settings.json` if you would rather not.

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
