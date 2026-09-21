# AeroThermalStudio — Tutorial

A step-by-step course, from installing the program to driving it from an AI
agent. Work through it in order the first time; each lesson builds on the one
before.

**Contents**

1. [Installation](#1-installation)
2. [Lesson 1 — First run and the demo](#lesson-1--first-run-and-the-demo)
3. [Lesson 2 — Your first rocket simulation](#lesson-2--your-first-rocket-simulation)
4. [Lesson 3 — Fin hinge torque](#lesson-3--fin-hinge-torque)
5. [Lesson 4 — Parametric sweeps](#lesson-4--parametric-sweeps)
6. [Lesson 5 — Making pictures](#lesson-5--making-pictures)
7. [Lesson 6 — The BMP580 sensor case](#lesson-6--the-bmp580-sensor-case)
8. [Lesson 7 — Projects and settings](#lesson-7--projects-and-settings)
9. [Lesson 8 — Driving it from an AI agent](#lesson-8--driving-it-from-an-ai-agent)
10. [Troubleshooting](#troubleshooting)

---

## 1. Installation

### What you need

| Requirement | Notes |
|---|---|
| Windows 11 | Windows 10 works; the analysis stack also runs on Linux and macOS |
| Python 3.11 or newer | From [python.org](https://www.python.org/downloads/). **Tick "Add Python to PATH"** in the installer |
| Microsoft MPI | For parallel solves. [Download page](https://www.microsoft.com/en-us/download/details.aspx?id=105289) — install **both** `msmpisetup.exe` and `msmpisdk.msi` |
| SU2 8.x | The flow solver. `Setup.bat` can fetch it, or install it yourself |
| ~4 GB free disk | Meshes and results add up quickly |

The reference machine is a 6-core / 12-thread CPU with 16 GB RAM. Everything
is tuned for that: 10 MPI ranks by default, meshes sized for 3–8 minute
solves.

### Steps

1. Put the `CFD-Program` folder somewhere permanent — for example
   `C:\AeroThermalStudio`. Avoid OneDrive or Documents folders that sync, as
   simulation output is large and changes constantly.
2. Double-click **`Setup.bat`**. It checks your machine, installs the Python
   packages and offers to fetch SU2. This takes a few minutes the first time.
3. Double-click **`Check-Environment.bat`**. You want to see:

   ```
   MPI launcher     : C:\Program Files\Microsoft MPI\Bin\mpiexec.exe
   SU2_CFD          : C:\Users\you\AppData\Local\AeroThermalStudio\su2\bin\SU2_CFD.exe

     Solver stack ready.
   ```

If SU2 shows `NOT FOUND`, that is not fatal — meshing, the sensor analysis and
all the rendering work without it. Only running a flow solve needs it. See
[Troubleshooting](#troubleshooting).

> **About the SU2 download.** `Setup.bat` refuses to install an archive it has
> no recorded SHA-256 checksum for, and prints manual instructions instead.
> That is deliberate: the downloaded file will execute on your machine, so
> installing it unverified is not a convenience worth having. Download SU2
> yourself from the [releases page](https://github.com/su2code/SU2/releases),
> extract it, and either put it in
> `%LOCALAPPDATA%\AeroThermalStudio\su2\bin` or set the `SU2_RUN` environment
> variable to the folder containing `SU2_CFD.exe`.

### The launchers

| File | What it does |
|---|---|
| `AeroThermalStudio.bat` | Starts the desktop program. This is the one you use daily |
| `Setup.bat` | First-time installation |
| `Check-Environment.bat` | Reports what is installed |
| `Run-Demo.bat` | Builds and meshes a sample rocket — no solver needed |
| `Run-MCP-Server.bat` | Starts the server for AI agents (usually launched by the AI client) |
| `Run-Tests.bat` | Runs the test suite |

---

## Lesson 1 — First run and the demo

**Goal:** confirm the installation works, before spending time on a real model.

Double-click **`Run-Demo.bat`**. It builds a finned sounding rocket in CAD,
aligns it, extrudes the boundary layer and meshes the flow domain. No solver
is involved, so this works even before SU2 is installed.

After a minute or so you should see something like:

```
Mesh report
----------------------------------------------
  Cells            : 252,303
  Nodes            : 109,810
  Target band      : 250,000 - 400,000
  Within band      : True
  Reference length : 0.9954 m
  Reference diam.  : 0.0800 m
  Achieved y+      : 45.0
  Minimum quality  : 0.380
  Markers          : {'WALL_ROCKET': 18030, 'WALL_FINS': 11686, 'FARFIELD': 406}
```

**How to read this:**

- **Cells / Within band** — the mesher automatically adjusted the cell size
  until the count landed inside the target range. That range is chosen so a
  solve takes 3–8 minutes on the reference machine.
- **Achieved y⁺ 45.0** — y⁺ measures how far the first cell centre sits from
  the wall in units the turbulence model cares about. The 30–60 band is where
  wall functions are valid. Getting this right is what makes an accurate
  solution affordable.
- **Minimum quality** — the worst cell in the mesh, as a fraction of a
  perfectly-shaped one. Above ~0.1 is fine; 0.38 is healthy.
- **Markers** — named surfaces the solver applies boundary conditions to.
  `WALL_ROCKET` is the airframe, `WALL_FINS` the fins, `FARFIELD` the outer
  boundary.

If you got this far, the installation is sound.

---

## Lesson 2 — Your first rocket simulation

**Goal:** go from a CAD file to drag and lift numbers.

### 2.1 Start the program

Double-click **`AeroThermalStudio.bat`**. You get a dark window with two tabs.
Stay on **Aerodynamics & Fins**.

### 2.2 Get a model

If you have no CAD to hand, use **File → Create sample rocket CAD**. It writes
a reference rocket and fills in the path for you.

Otherwise, drag your `.step` or `.stp` file onto the window, or use **Browse**.

> **Your CAD must be a solid**, not a collection of surfaces. If your modeller
> produced a surface model, sew it into a solid before exporting. The program
> will tell you clearly if it finds no solid.

### 2.3 Tell the program which way is forward

This is the step people most often get wrong, and everything downstream
depends on it.

The program needs to know which direction the **nose** points in *your CAD
file's own coordinate system*. Click the matching button: `+X`, `-X`, `+Y`,
`-Y`, `+Z` or `-Z`.

If your model is not aligned to an axis, tick **Use custom vector** and type
the direction, for example `0.0, 0.3, 0.95`. It is normalised for you, so
magnitude does not matter — only direction.

**Reference origin** is the point that gets moved to the origin of the wind
tunnel. Use the nose tip. Everything reported afterwards — especially the
centre of pressure — is measured from here, so a sensible choice makes the
results easy to interpret.

**Scale to metres** converts your CAD units. If you modelled in millimetres,
set this to `0.001`. Getting this wrong gives a body a thousand times too
large and nonsense Reynolds numbers.

### 2.4 Set the flow domain

The three sliders control how much air is simulated around the body, in
multiples of the body length:

| Slider | Default | What it does |
|---|---|---|
| Upstream | 5 L | Distance from the nose to the inlet |
| Downstream | 10 L | Distance from the base to the outlet |
| Radial | 5 L | Distance from the axis to the outer boundary |

The defaults are sound for most work. Downstream is larger because the wake
needs room to develop before it reaches the boundary. Make the domain bigger
if you see the solution being disturbed at the edges; make it smaller only if
you are short of cells and know the flow is well behaved.

Choose **cylinder** for rockets (it wastes fewer cells on a slender body) and
**box** for enclosures.

### 2.5 Set the flight condition

- **Speed as** — Mach number or true airspeed in m/s.
- **Speed** — for a supersonic rocket, try 2.0.
- **Angle of attack** — how much the nose is pitched relative to the airflow,
  in degrees. Range ±20°.
- **Sideslip** — the same idea in yaw.
- **Altitude** — sets air pressure, temperature and density from the standard
  atmosphere.

Beneath the sliders is a live readout:

```
M 2.000  |  V 680.6 m/s  |  p 101.3 kPa  |  T 288.2 K
```

Check it looks right before continuing. It is the quickest way to catch a unit
mistake.

### 2.6 Choose the mesh

| Setting | Guidance |
|---|---|
| **Resolution** | Start `coarse`. Move to `medium` or `fine` once the setup is right |
| **Prism layers** | 7 is a good default (range 5–8) |
| **Target y⁺** | Leave at 45 unless you know why you want otherwise |
| **MPI ranks** | Two fewer than your thread count. 10 on a 12-thread CPU |

### 2.7 Mesh it

Click **Generate mesh**. The log shows progress:

```
meshing attempt 1 (size scale 1.000)
classified 4 airframe and 17 fin faces (airframe radius 40.0 mm)
generating surface mesh
extruding prism layers from 22136 wall triangles
generating tetrahedral farfield mesh
Mesh ready: 190,070 cells, y+ 45.0, quality 0.380
```

**Check the classification line.** It says how many surfaces were treated as
airframe versus fins. The split is decided by how far each surface reaches
from the body axis. If your model has an unusual shape and the split looks
wrong, the numbers are still valid — only the reporting of forces per surface
is affected.

If the cell count lands outside the target band, the program automatically
remeshes with an adjusted cell size, up to four times.

### 2.8 Solve it

Click **Run simulation**. The residual chart fills in as the solver runs.

**Reading the chart:** `RMS[Rho]` is the density residual on a log scale. It
should fall steadily. Dropping five orders of magnitude (from −1 to −6, say)
means well converged. `C_d` and `C_l` should flatten out — that is usually the
better sign, because forces often settle before the residual target is
reached, and the program stops on either condition.

When it finishes, the result cards fill in:

| Card | Meaning |
|---|---|
| **Drag coefficient (C_d)** | Dimensionless drag. For a slender rocket at Mach 2, expect roughly 0.3–0.6 |
| **Lift coefficient (C_l)** | Zero at zero angle of attack on a symmetric body |
| **Drag force / Lift force** | The same, in newtons at your flight condition |
| **Hinge torque** | See Lesson 3 |
| **Centre of pressure** | Axial station where the aerodynamic force effectively acts |

> **Centre of pressure shows `--`?** That is correct at zero angle of attack.
> With no sideways force there is no defined centre of pressure, and the
> program says so rather than inventing a number. Set the angle of attack to
> 2–5° and run again.

### 2.9 Sanity-check the answer

Before you trust any CFD result:

1. **Is the order of magnitude right?** Compare against a hand calculation or
   published data for a similar shape.
2. **Did it converge?** The log says so explicitly.
3. **Does it change with the mesh?** Run again at `medium` resolution. If C_d
   moves by more than a few percent, the mesh is not fine enough. This is
   called a mesh independence study and it is not optional for work that
   matters.

---

## Lesson 3 — Fin hinge torque

**Goal:** size the servo that holds a steering fin.

This is the question a control system designer actually needs: *how hard does
the air push back on my fin, about the axis it rotates on?*

### 3.1 Define the hinge

In the **Fin hinge axis** panel:

- **Name** — a label, e.g. `fin_pitch`.
- **Point** — any point on the hinge line, in metres, in the aligned frame
  (nose tip at the origin, body axis along +X). For a fin at 0.9 m from the
  nose, attached at the 40 mm body radius: `0.9, 0.04, 0.0`.
- **Direction** — the axis the fin rotates about. A fin mounted in the
  horizontal plane rotating to change pitch has direction `0, 1, 0`.

Click **Show axis in 3D** to see the line drawn in the viewport. Getting this
visually right is much easier than checking numbers.

### 3.2 Understand what is reported

The program computes the full aerodynamic moment, transfers it to your hinge
point, and projects it onto your hinge direction. The result is a single
number in newton-metres: the torque your servo must hold.

Two properties worth knowing, both verified by tests:

- A force acting exactly **through** the hinge line produces **zero** torque,
  no matter where the solver's own moment reference sits.
- Reversing the direction vector reverses the sign. The magnitude is what
  sizes the servo; the sign tells you which way it pushes.

### 3.3 Size the servo

One simulation gives you the torque at one flight condition. What you need is
the **worst case** across the whole envelope — which is Lesson 4.

A rule of thumb: pick a servo rated for at least twice the peak aerodynamic
torque, to cover dynamic overshoot, friction and manufacturing variation.

---

## Lesson 4 — Parametric sweeps

**Goal:** get a curve instead of a point.

### 4.1 Set it up

At the bottom of the control panel:

- **Sweep parameter** — `mach`, `aoa`, `sideslip` or `altitude`.
- **Sweep values** — a comma-separated list, e.g. `0.5, 1.0, 1.5, 2.0, 2.5, 3.0`.

Everything else stays at whatever the other controls say. So to get a drag
polar at Mach 2, set the speed to 2.0, sweep `aoa` over
`-10, -5, 0, 5, 10`.

### 4.2 Run it

Click **Batch sweep**. Points run one after another — deliberately, because
16 GB of RAM will not hold two large solutions at once.

Progress appears per point:

```
point 1/6: mach=0.5 (ok)
point 2/6: mach=1 (ok)
point 3/6: mach=1.5 (ok)
```

A point that fails to converge is recorded and the sweep continues. One bad
condition does not throw away an expensive batch.

### 4.3 Read the result

The log reports the peak values, including:

```
peak hinge torque 0.0840 N m
```

That is the number to size the servo from — the worst case across everything
you swept.

### 4.4 A practical sequence

For a real fin-sizing exercise:

1. Sweep Mach at your maximum expected angle of attack. Torque usually peaks
   near the highest dynamic pressure, not the highest Mach.
2. At the worst Mach, sweep angle of attack to find the true peak.
3. Add 100% margin and choose the servo.

---

## Lesson 5 — Making pictures

**Goal:** understand the flow, and produce figures for a report.

Four kinds of image are available:

| Mode | Shows | Use it for |
|---|---|---|
| `surface_pressure` | Pressure or C_p on the body | Where the loads are |
| `mach_slice` | Mach number on a cutting plane | Shock waves and expansion fans |
| `streamlines` | Flow paths | Separation, and airflow into the sensor intake |
| `thermal` | Temperature contours | The sensor case |

In the viewport, use the **Colormap** and **View** selectors. For publication
images, render at 4K through the MCP tool or from a script.

**Reading a Mach slice of a supersonic rocket:**

- A **bow shock** stands off the nose. At Mach 2 with a sharp cone it sits
  close to the tip; a blunt nose pushes it further forward and costs more drag.
- Mach **increases** around the shoulder where the nose meets the body — that
  is a Prandtl–Meyer expansion fan.
- Each fin leading edge makes its **own oblique shock**.
- Behind the base is a low-speed **wake**, and base drag can be a third of the
  total at supersonic speed.

---

## Lesson 6 — The BMP580 sensor case

**Goal:** find out how far off a temperature sensor reads, and what to do
about it.

This track answers a different question from the rocket work: a BMP580 sits
inside a 3D-printed housing, fed by a ram-air sampling tube, mounted above a
sun-heated tram roof. **How much does it read above true air temperature?**

Switch to the **Sensor Microclimate (BMP580)** tab.

### 6.1 The prediction is instant

Unlike the rocket track, this tab shows an answer immediately and updates as
you move sliders. It solves the coupled energy balances — roof, housing, tube
flow and sensor chamber — analytically, in milliseconds.

With the defaults (2 m/s, 25 °C, 800 W/m², 0.1 m above the roof):

| Readout | Value |
|---|---|
| Sensor reads | 26.97 °C |
| True ambient | 25.00 °C |
| **Measurement error** | **+1.97 K** |
| Roof surface | 63.4 °C |
| Housing | 28.7 °C |
| Intake flow | 92.7 mg/s |
| Roof thermal layer | 15.4 mm |

The error readout is colour-coded: green below 0.5 K, amber below 2 K, red
above.

### 6.2 What the numbers mean

**Roof at 63 °C.** A dark roof under 800 W/m² of sun with only 2 m/s of
airflow really does get that hot. This is the source of the problem.

**Roof thermal layer 15.4 mm.** This is the key geometric number. Air within
about 15 mm of the roof has been warmed by it. Your intake is at 100 mm, so it
is sampling genuinely ambient air — the panel says so in plain words.

**Error +1.97 K anyway.** If the intake air is ambient, why is the reading
high? Because the sun also heats the *housing*, and the air warms as it passes
through the chamber on its way to the sensor. That is the dominant term.

**Self-heating is negligible.** The BMP580's own 1 mW contributes about 5 mK —
a thousandth of the error. Worth knowing: the problem is environmental, not
the sensor.

### 6.3 Experiment

Drag the sliders and watch:

| Change | Effect | Why |
|---|---|---|
| Solar flux → 0 (night) | Error goes **negative**, about −0.5 K | The roof and housing radiate heat to the cold sky and end up below ambient |
| Speed → 0 (stopped) | Error jumps to about **+13 K** | No ram air to flush the chamber, and the intake now sits inside the roof's plume |
| Speed → 10 m/s | Error falls to about **+0.5 K** | Faster flushing |
| Height → 0.01 m | Error roughly **triples** | The intake is now inside the roof's thermal layer |
| Housing absorptivity 0.3 → 0.1 | Error roughly **halves** | A white or reflective housing absorbs much less sun |

### 6.4 What to do about it

In order of effectiveness per unit of effort:

1. **Make the housing white or reflective.** Absorptivity 0.3 → 0.1 roughly
   halves the error. Cheapest possible fix.
2. **Keep the intake above the roof's thermal layer.** Check the reported
   layer thickness at your slowest expected speed, and mount well above it.
3. **Shade the housing**, if the installation allows.
4. **Widen the intake tube.** More flushing flow pulls the reading towards
   ambient.
5. **Insulate between housing and chamber**, so wall heat couples less into
   the sampled air.

Remember the worst case is **stationary in full sun** — a tram at a stop. If
your application cares about that condition, design for it.

### 6.5 The full CFD version

The instant answer uses a lumped model. For a full 3D conjugate heat transfer
solution — temperature fields throughout the solid housing and the internal
air — load the enclosure CAD, mesh it on the thermal track, and run the
conjugate solver. That resolves detail the lumped model averages over.

---

## Lesson 7 — Projects and settings

**Goal:** stop losing your work.

### 7.1 Projects

A project stores **everything**: CAD path and orientation, domain, mesh
settings, flight condition, solver settings, hinge axes, the sensor scenario,
the sweep definition and your notes.

| Action | How |
|---|---|
| Save | **File → Save project** (Ctrl+S) |
| Save under a new name | **File → Save project as** |
| Open | **File → Open project** (Ctrl+O) |
| Reopen a recent one | **File → Open recent** |
| Start fresh | **File → New project** (Ctrl+N) |
| Edit name and notes | **File → Project details** |

Projects are plain JSON with the extension `.atsproj`. You can read them in a
text editor, diff them in version control, and commit them next to the CAD
they refer to. Saving is atomic — an interrupted save cannot corrupt an
existing file.

**Use the notes field.** Six weeks later you will not remember why the domain
was 8 L upstream. Write it down.

### 7.2 Settings

**Settings → Preferences** holds things that are not part of any one study:
default MPI ranks, default colormap and resolutions, and behaviour toggles.
These persist between sessions.

**Settings → Open data folder** reveals where meshes, results and projects are
stored — `%LOCALAPPDATA%\AeroThermalStudio` by default. Simulation output is
large; this is where to look when you need to reclaim disk space.

---

## Lesson 8 — Driving it from an AI agent

**Goal:** have an assistant run studies for you.

Everything in the GUI is also available over the Model Context Protocol, so an
AI assistant can prepare geometry, mesh, solve, sweep and render.

The short version: point your AI client at `Run-MCP-Server.bat`, then ask for
what you want in plain language —

> "Mesh the rocket at `C:\models\rocket.step` with the nose along +X, then
> sweep Mach from 0.5 to 3.0 in six steps at 5° angle of attack, with a fin
> hinge at [0.9, 0.04, 0] rotating about [0, 1, 0]. Tell me the peak hinge
> torque."

The full setup, the tool reference and worked agent workflows are in
[MCP_AI_GUIDE.md](MCP_AI_GUIDE.md).

---

## Troubleshooting

### "Python was not found"

Install Python 3.11+ from python.org and **tick "Add Python to PATH"** during
installation. Then run `Setup.bat`.

### "SU2_CFD was not found"

Meshing, the sensor analysis and rendering all work without it — only flow
solves need it. To install:

1. Download a Windows build from the
   [SU2 releases page](https://github.com/su2code/SU2/releases).
2. Extract it, for example to `C:\SU2`.
3. Set `SU2_RUN` to the folder containing `SU2_CFD.exe`:
   ```
   setx SU2_RUN "C:\SU2\bin"
   ```
4. Open a **new** terminal and run `Check-Environment.bat`.

### "a 10-rank run needs an MPI launcher"

Install Microsoft MPI — **both** `msmpisetup.exe` and `msmpisdk.msi` — and
reopen your terminal. Or set MPI ranks to 1 for a serial solve, which is
slower but works.

### The CAD file will not import

- The program needs a **solid**, not surfaces. Sew your model into a solid
  before exporting.
- Check **Scale to metres**: `0.001` for millimetres.
- Very complex assemblies can take a long time to heal. Simplify away internal
  detail the flow never sees.

### Meshing fails on the boundary layer

Usually a mathematically sharp feature — a knife-edge or a perfect point — that
cannot be meshed cleanly. Add a small radius, even 0.1 mm. Real parts have one
anyway.

If it succeeds but reports frozen vertices, that is normal and local: a handful
of points get a thinner boundary layer while the rest is unaffected.

### The cell count will not reach the target band

Raise **max targeting iterations**, or change the resolution preset. Very
small or very large models sometimes need the domain multipliers adjusted too.

### The solve diverges

- Lower the **CFL number** (through a project file or the MCP tools). 5 is the
  default; try 2.
- Check the mesh quality. Below about 0.05 is trouble.
- Check your flight condition is physical — a very high angle of attack at high
  Mach may simply have no steady solution.

### Results look wrong

1. Check the derived condition readout (`M ... | V ... | p ... | T ...`).
2. Check **Scale to metres**.
3. Check the nose direction — a body meshed backwards gives plausible-looking
   but wrong numbers.
4. Do a mesh independence study before trusting anything.

### Where are my files?

**Settings → Open data folder**, or `%LOCALAPPDATA%\AeroThermalStudio`:

```
runs/        one folder per mesh, simulation and sweep
projects/    saved .atsproj files
samples/     generated sample CAD
settings.json
```

---

## Where to go next

- **[USER_GUIDE.md](USER_GUIDE.md)** — reference for every setting, with
  guidance on choosing values.
- **[MCP_AI_GUIDE.md](MCP_AI_GUIDE.md)** — the full AI agent interface.
- **[../../README.md](../../README.md)** — architecture and technical notes.
