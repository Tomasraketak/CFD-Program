# AeroThermalStudio

A native Windows 11 CFD and thermal simulation platform for two jobs:

1. **Rocket and fin aerodynamics** — drag, lift, centre of pressure and fin
   hinge torque from Mach 0.2 to 3.5, at arbitrary angle of attack and
   sideslip.
2. **Sensor microclimate analysis** — what a BMP580 actually reads inside a
   3D-printed enclosure with a ram-air sampling tube, mounted above a
   solar-irradiated surface such as a tram roof — and **radiation-shield
   studies**: a Design of Experiments over wind, sun and ground/roof
   radiation, a response surface and a Monte Carlo analysis of the
   thermometer's error, solved analytically, with SU2, or in ANSYS Fluent
   from an exported Workbench package.

Every setting is controllable two ways: through the desktop GUI, and
programmatically by an AI agent over the Model Context Protocol. The same
tools also back an assistant **inside** the program, so an operator can type
"sweep Mach 0.5 to 3 and tell me the worst-case hinge torque" and watch it
happen.

---

## Documentation

Full guides are in **[docs/](docs/README.md)**, in English and Czech
(anglicky a česky):

| | English | Česky |
|---|---|---|
| Step-by-step course | [Tutorial](docs/en/TUTORIAL.md) | [Tutoriál](docs/cs/TUTORIAL.md) |
| Every setting explained | [User Guide](docs/en/USER_GUIDE.md) | [Uživatelská příručka](docs/cs/USER_GUIDE.md) |
| Radiation shield study with Ansys Student, from scratch | [Shield Study Guide](docs/en/SHIELD_STUDY_GUIDE.md) | [Studie radiačního štítu](docs/cs/SHIELD_STUDY_GUIDE.md) |
| AI agent interface | [MCP and AI Guide](docs/en/MCP_AI_GUIDE.md) | [MCP a AI](docs/cs/MCP_AI_GUIDE.md) |

---

## Quick start

On Windows, double-click the launchers:

| File | What it does |
|---|---|
| `Setup.bat` | Check the machine, install dependencies, fetch SU2 |
| `Run-Demo.bat` | Build and mesh a sample rocket — no solver needed |
| `AeroThermalStudio.bat` | Start the desktop application |
| `Check-Environment.bat` | Report the detected toolchain |
| `Run-MCP-Server.bat` | MCP server for AI agents |
| `Run-Tests.bat` | Run the test suite |

Or from a terminal on any platform:

```bash
python setup_env.py          # check the machine, install dependencies, fetch SU2
python run_app.py --check    # report the detected toolchain
python run_app.py --demo     # build the sample rocket and mesh it (no SU2 needed)
python run_app.py --gui      # desktop application
python run_app.py --mcp      # MCP server on stdio
```

`--demo` is the fastest way to confirm the install: it generates a finned
rocket in CAD, aligns it, extrudes prism boundary layers, meshes the farfield
and writes a tagged `.su2` file, all without the solver.

---

## Install, update and run from a terminal

The whole thing from an empty terminal on Windows 11.

**Which terminal are you in?** Windows 11 opens **PowerShell** by default —
the prompt starts with `PS`. Command Prompt shows a bare `C:\...>`. The two
disagree about environment variables and about how a script is activated, so
each step below gives both where they differ. Everything else is identical.

### Step 1 — Python and Git

Install [Python 3.11 or newer](https://www.python.org/downloads/), ticking
**Add python.exe to PATH** on the first installer screen, and
[Git for Windows](https://git-scm.com/download/win). Then open a **new**
terminal — PATH changes do not reach one that is already open — and check:

```powershell
python --version
git --version
```

You want `Python 3.11.x` or higher and any Git version. If instead you get
*"Python nebyl nalezen"* / *"Python was not found"*, or the Microsoft Store
opens, Python is **not installed**: what answered was the Store placeholder
that Windows ships in place of it. Install from python.org, with the PATH box
ticked, and open a new terminal. (If it still answers after that, turn off
**Settings → Apps → Advanced app settings → App execution aliases →
python.exe / python3.exe**.)

Do not continue until both commands print a version. Nothing below can work
before they do.

### Step 2 — Install

```powershell
# PowerShell
cd $env:USERPROFILE
git clone https://github.com/Tomasraketak/CFD-Program.git
cd CFD-Program

python -m venv .venv
.\.venv\Scripts\Activate.ps1

python setup_env.py
```

```cmd
:: Command Prompt
cd %USERPROFILE%
git clone https://github.com/Tomasraketak/CFD-Program.git
cd CFD-Program

python -m venv .venv
.venv\Scripts\activate.bat

python setup_env.py
```

Activation worked when the prompt gains a `(.venv)` prefix. If PowerShell
answers *"running scripts is disabled on this system"*, allow it for this
window only and try again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

You can also skip activation entirely and call the environment's Python by
path — `.\.venv\Scripts\python.exe setup_env.py` — which needs no policy
change at all.

The virtual environment is optional but worth having: it keeps these packages
out of your system Python, and the `.bat` launchers find `.venv` on their own,
so after this you can double-click them without activating anything.

`setup_env.py` checks the machine, installs the Python packages, and offers to
fetch SU2. It **refuses to install an SU2 archive with no recorded SHA-256**
and prints manual instructions instead — silently installing an unverified
binary that will then execute on your machine is not a convenience worth
having. Microsoft MPI is a separate download it will point you at.

### Step 3 — Confirm it works

```powershell
python run_app.py --check
python run_app.py --demo
```

`--check` lists what was found; `--demo` meshes a sample rocket end to end and
needs no solver. If `--demo` produces a `.su2` file, the install is sound even
when SU2 itself is still missing.

### Step 4 — MS-MPI and SU2 (only needed to solve)

`setup_env.py` finishes with `MPI: missing` and `SU2: missing` on a fresh
machine, and reports the setup incomplete. That is expected: neither can be
installed from PyPI. Everything except the CFD solve — the GUI, meshing,
visualisation, the analytical sensor model, the AI assistant — works without
them, so skip this step until you actually want to run a flow solution.

**MS-MPI.** Download both files from
[Microsoft MPI](https://www.microsoft.com/en-us/download/details.aspx?id=105289)
and run them in this order:

1. `msmpisetup.exe` — the runtime, which provides `mpiexec`
2. `msmpisdk.msi` — the SDK

Then close the terminal and open a **new** one, otherwise the updated `PATH`
is not visible to it.

**SU2.** No SHA-256 checksum is recorded for the Windows build in this
repository, so the installer refuses to fetch it rather than run an unverified
download. Two honest ways forward:

```cmd
:: Let the installer fetch it anyway, checksum unverified
python setup_env.py --allow-unverified
```

or install it by hand — download `SU2-v8.1.0-win64-mpi.zip` from the
[SU2 releases page](https://github.com/su2code/SU2/releases) and extract it so
that the executables land in

```
%LOCALAPPDATA%\AeroThermalStudio\su2\bin\SU2_CFD.exe
```

The `bin` subdirectory matters — that is exactly where the program looks. If
you would rather keep SU2 elsewhere, point the environment variable at
whichever directory holds `SU2_CFD.exe` and open a new terminal:

```cmd
setx SU2_RUN "C:\path\to\SU2\bin"
```

Confirm with `python run_app.py --check`; both lines should turn into paths.

### Run

```powershell
# PowerShell
cd $env:USERPROFILE\CFD-Program
.\.venv\Scripts\Activate.ps1
python run_app.py --gui
```

```cmd
:: Command Prompt
cd %USERPROFILE%\CFD-Program
.venv\Scripts\activate.bat
python run_app.py --gui
```

Or just double-click `AeroThermalStudio.bat`, which does all of that for you
and needs no terminal at all.

| Command | What it does |
|---|---|
| `python run_app.py --gui` | Desktop application (the default with no flag) |
| `python run_app.py --check` | Report the detected toolchain |
| `python run_app.py --check --json` | Same, machine-readable |
| `python run_app.py --demo` | Mesh the sample rocket, no solver needed |
| `python run_app.py --mcp` | MCP server on stdio, for an external AI client |
| `pytest -q` | Run the test suite |

### Update

**The one-line shortcut**, once the program is installed in
`%USERPROFILE%\CFD-Program`. Paste it into a *Command Prompt* (`cmd`, not
PowerShell — `cd /d`, `&&` and `call` are cmd syntax): it pulls the latest
version, activates the environment, brings the packages and SU2 up to date,
and starts the program.

```bat
cd /d "%USERPROFILE%\CFD-Program" && git pull && call .venv\Scripts\activate.bat && python setup_env.py && python run_app.py --gui
```

Each step runs only if the one before it succeeded, so a failed `git pull`
stops there rather than starting an old version.

Or step by step — activate as above, then:

```powershell
git pull
pip install -r requirements.txt --upgrade
python run_app.py --check
```

Run `python setup_env.py` again instead of `pip install` if a release note
says the SU2 version changed.

Your work is not in the repository and a `git pull` cannot touch it: projects,
meshes, results and settings live in `%LOCALAPPDATA%\AeroThermalStudio`.
If `git pull` complains that local changes would be overwritten, you have
edited the program's own files — `git stash` puts them aside, `git stash pop`
brings them back after the pull.

### If something goes wrong

| Symptom | Cause and fix |
|---|---|
| *Python nebyl nalezen* / *Python was not found*, or the Store opens | Python is not installed — that is the Windows Store placeholder answering. Step 1 |
| `No suitable Python runtime found` from `py` | Same thing: the launcher is there, a Python is not. Step 1 |
| `'python' is not recognized` | Installed but not on PATH. Reinstall with **Add python.exe to PATH** ticked, or use `py` |
| `'git' is not recognized` | Install Git for Windows, then open a *new* terminal |
| `cd : Cannot find path '...\%USERPROFILE%'` | `%USERPROFILE%` is cmd syntax. In PowerShell write `$env:USERPROFILE` |
| `The module '.venv' could not be loaded` | PowerShell needs `.\.venv\Scripts\Activate.ps1` — the leading `.\` and the `.ps1` both matter |
| `running scripts is disabled on this system` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate again |
| Activation printed nothing | It worked — look for the `(.venv)` prefix on the prompt |
| `--check` reports no SU2 or MPI | The GUI, meshing and the analytical sensor model still work; only the CFD solve needs them. Step 4 |
| `Still missing: mcp` after setup | A single package failed to install. Re-run `python -m pip install mcp` inside the activated venv and read the error it prints |
| `no SHA-256 checksum is recorded for SU2` | Deliberate: the installer will not fetch an unverified archive. Step 4 |
| SU2 installed but still `NOT FOUND` | `SU2_CFD.exe` is not in `...\AeroThermalStudio\su2\bin`, or `SU2_RUN` was set without opening a new terminal |

---

## How it fits together

The defining constraint is that the GUI and an AI agent must reach the same
capabilities. Rather than maintaining two parallel interfaces, both are thin
clients over one **typed parameter core**:

```
                    core/models.py  (Pydantic v2)
                    every setting, once, with
                    bounds + units + description
                             |
        +--------------------+--------------------+
        |                    |                    |
   GUI forms           MCP tool schemas      SU2 config files
 (gui/form_builder)   (model_json_schema)   (backend/su2_config)
```

A field gains a bound or a description in one place and all three pick it up.
`core/store.py` is a shared run registry keyed by `mesh_id` / `sim_id`, so a
mesh an agent produced opens in the GUI and vice versa. `core/project.py`
bundles a whole setup into a `.atsproj` JSON document — plain text, diffable,
and worth committing beside the CAD it refers to — so a study can be put down,
handed over, and picked up weeks later.

The same principle covers the smaller hand-off. Importing a STEP file in the
interface records it in `core/workspace.py`, so "mesh the model I just
imported" reaches the assistant as a real file rather than a request for a
path.

`core/step_inspect.py` then reads the file itself, without a CAD kernel, to
settle the two questions that fail silently:

- **What unit is this drawn in?** CAD is usually exported in millimetres, the
  solver works in metres, and a body scaled wrong by three orders of
  magnitude meshes and solves without a single warning. The declaration is
  read and then checked against the model's own size, because exporters lie:
  OpenCASCADE writes a millimetre header onto a model whose coordinates are
  plainly metres, this repository's own sample rocket among them.
- **Which way does it point?** The longest axis is the body axis; the end
  that tapers is the nose, since a tail carries fins and a blunt base. Get
  this wrong and the rocket is meshed flying backwards, which produces a
  complete and entirely plausible drag polar.

Neither answer is forced. When the ends are alike — a plain tube, a boat tail
— the interface says so and the meshing tool fails asking which end it is,
rather than picking one. A guess that cannot be detected downstream is worse
than a question.

```
core/       models  store  units  atmosphere  platform_env
            project  settings  credentials  workspace  step_inspect
backend/    mesh_pipeline  prism_layers  su2_mesh  sample_geometry
            aero_solver  thermal_solver  su2_config  su2_parser
            runner  sweep  visualizer  ai_agent
gui/        main_window  ai_panel  form_builder  workers  theme
docs/       en/  cs/            tutorial, user guide, MCP guide
*.bat       Windows launchers, sharing _launcher.cmd
mcp_server.py   run_app.py   setup_env.py   tests/
```

---

## The meshing problem, and what was done about it

**Gmsh cannot generate 3D prism boundary layers.** Its `BoundaryLayer` field
is 2D-only (curves and points, no surfaces), and `geo.extrudeBoundaryLayer`
fails with *"Invalid boundary mesh (overlapping facets)"* on even a closed
cylinder.

That is not a cosmetic limitation. Holding y⁺ ≈ 45 on a 1 m body with
isotropic tetrahedra needs wall cells of ~6.5 × 10⁻⁵ m — millions of surface
triangles alone, roughly 5× past the 750k ceiling that keeps a solve inside
the 3–8 minute target. Anisotropic prisms are mandatory.

`backend/prism_layers.py` therefore implements advancing-layer extrusion
directly: layers march along area-weighted, Laplacian-smoothed vertex normals,
constrained by three independent limiters (normal visibility, KD-tree
proximity to non-adjacent surface, and a thickness cap). Vertices that would
still collide are frozen, giving a locally thinner stack rather than tangled
cells.

Three failure modes surfaced on real geometry and are handled explicitly:

| Problem | Cause | Fix |
|---|---|---|
| Collapsed columns at a cone apex | Incident face normals cancel exactly, leaving a zero vertex normal | Fall back to the one-ring centroid direction |
| Prisms shearing into inversion near slender tips | Laplacian smoothing averages radial normals to zero and drags the march onto the body axis | Spherically clamp smoothing to 30° from each vertex's own normal |
| One singular vertex discarding a whole good layer | Inverted-layer repair halved *every* step | Repair only the offending vertices; freeze them on the final attempt |

Verified on the finned reference rocket: **7/7 layers, 289,940 prisms in
2.9 s, 5 of 20,715 vertices frozen, every cell of positive volume.**

The surrounding pipeline bisects the characteristic cell size until the count
lands inside the target band — that loop is what makes the cell-count
guarantee real rather than a hope. On the reference rocket it converges in
3 iterations to **252,303 cells inside the 250k–400k band at y⁺ 45.0.**

Real exported CAD adds three more failure modes, all of them found on one
operator's rocket and all handled without asking them to re-export:

| Problem | Cause | Fix |
|---|---|---|
| `Impossible to mesh periodic surface` | Gmsh meshes a face that closes on itself through a separate path, which gives up on an imported airframe whose seam is interrupted by fin roots | Cut the body on two planes through its axis and mesh the open quarters; the cut faces are interior and never reach the wall markers |
| Import reported no solid, on a file that plainly has one | Sewing was enabled for the import, and sewing a solid's faces yields a shell — OpenCASCADE does not promote it back | Sew only when the plain import finds no solid, then cap the shell and check that it encloses a volume |
| A healed body lost its nose cone to the axial cut | Healing subdivides faces, and the boolean discarded a piece instead of dividing it | Skip healing when splitting, and refuse any split that changes the body's extent |

The last one is the reason for the extent check rather than a comment. A
body that quietly loses 200 mm still meshes, still solves, and returns drag
figures that look entirely reasonable.

Two of those recoveries cost time as well as clarity. The closed-face split
used to be rediscovered on every pass of the cell-count loop — up to five
doomed surface meshes on one body — so the answer is now remembered for the
rest of the run. And every long step announces itself and reports its own
duration, because a Gmsh call that runs for four minutes in silence is
indistinguishable from a hung program. Measured on the rocket that prompted
this, at the interface's own defaults: **126.3 s → 101.6 s for the same
429,832-cell mesh.**

---

## Aerodynamics

The convective scheme follows the regime: JST central differencing below
Mach 0.8, Roe upwind with MUSCL reconstruction and the Venkatakrishnan
limiter above it (plus an entropy fix so Roe cannot admit expansion shocks),
with SST k-ω throughout and wall functions matching the y⁺ 30–60 mesh.

**Rocket axes.** Whichever way the CAD was drawn, the rocket is shown and
reported standing up, nose along +Z: the viewport, rendered images, the
force along each axis, the centre of pressure and the fin hinges. The mesh
itself stays in the solver's frame, body along +X, because SU2's angle of
attack assumes it; the two differ by one fixed rotation (`core/frames.py`),
so nothing is re-meshed to stand the rocket up. Drag on a rocket climbing
nose-first comes out as a negative F_z.

**Hinge torque** is computed here rather than taken from SU2, because a fin
hinge is an arbitrary line the solver knows nothing about. The aerodynamic
moment is transferred to the hinge point with the r × F parallel-axis
correction and projected onto the hinge axis, so a force acting through the
hinge line yields exactly zero torque regardless of where the solver's moment
origin sits.

Convergence stops on *either* the residual threshold or steady forces — a
RANS solve often reaches usable forces well before the residual target, and
the forces are what the operator wants.

**Starting a supersonic case** is the fragile part, and the numerics say so.
The starting CFL and its adaption follow the regime, and no regime doubles
it any more: low subsonic starts at CFL 5 and grows 15% per iteration, from
Mach 0.6 the start is CFL 2 growing 10%, and from Mach 1.2 it starts at CFL 1
and climbs 5% per iteration. The subsonic ramp used to double too, and a
Mach 0.7 case diverged on three meshes before that was found; the rescue
had been no help, because its "first order" did nothing to JST and its low
starting CFL was back at 100 eight iterations later. It now runs genuinely
first-order Roe with the ramp held down, and scheme, start, growth and
ceiling can all be set from the GUI and over MCP. Doubling is what killed a Mach 1.3 run at
iteration six — from CFL 5 it is past 150 by then, far beyond what the linear
solve can follow, at which point the implicit update is a badly under-relaxed
explicit one and a prism cell goes negative. The upwind branch also uses
Green-Gauss gradients, which are less accurate than weighted least squares
but cannot blow up on the sliver cells where a hybrid mesh changes character.

When a solve diverges anyway, it is caught at the iteration that went
non-finite — not at the iteration cap, and not by hanging — and retried
automatically: first order at low CFL to establish the shock, then a
second-order restart from that solution. The result carries `rescued` and a
note explaining it, because a rescued number is valid but the case needed
help, which usually says something about the mesh.

**A solve can no longer sit silently.** It has a stall deadline (no output at
all) and a wall-clock deadline, checked on a timer rather than only when a
line arrives, and whichever fires kills the whole process tree. The failure
this replaces was specific: an aborted MPI job left a rank holding the
inherited stdout pipe, killing the launcher alone did not reach it, and the
read blocked forever — fifteen minutes at 1% CPU with nothing running.

---

## Sensor microclimate

Two paths to the answer.

`LumpedThermalModel` solves the coupled roof, housing, tube-flow and chamber
energy balances analytically in milliseconds. It seeds the CFD run's radiative
linearisation, bounds it in tests, and drives the GUI readout live as sliders
move.

`run_thermal_case` runs the SU2 multizone conjugate solve and probes the
solution at the die coordinate. Radiation is the awkward part — fourth-power
in temperature, while SU2's wall-flux condition is linear — so the radiative
term is linearised about the current wall temperature and refreshed between
outer iterations. The linearisation is exact, not approximate:
`h_r·(Ts − Tsky)` reproduces `εσ(Ts⁴ − Tsky⁴)` identically.

For the specified case — 2 m/s, 25 °C, 800 W/m², 0.1 m above a tram roof:

| Quantity | Value |
|---|---|
| Roof surface | 63.4 °C |
| Housing | 28.7 °C |
| **Sensor reads** | **26.97 °C** |
| **Measurement bias** | **+1.97 K** |
| Intake mass flow | 92.7 mg/s |
| Roof thermal boundary layer | 15.4 mm |

The bias is dominated by coupling to the sun-warmed housing walls, not by the
sensor's own 1 mW, which contributes about 5 mK. At 2 m/s the roof's thermal
layer is ~15 mm, so an intake 100 mm up samples genuinely ambient air;
dropping it to 5 mm puts it inside that layer and roughly triples the error.
A lighter housing finish roughly halves the bias. The model reports which
regime it is in.

---

## MCP tools

| Tool | Purpose |
|---|---|
| `set_geometry_and_mesh` | CAD → aligned, cell-count-targeted mesh |
| `run_aerodynamic_simulation` | Forces, coefficients, CoP, hinge torques |
| `run_sensor_thermal_simulation` | BMP580 reading and bias (`analytic_only` for an instant answer) |
| `radiation_shield_study` | Radiation-shield DoE → response surface → Monte Carlo; SU2 cases and a Fluent/Workbench package |
| `generate_cfd_visualization` | Surface pressure, Mach slice, streamlines, thermal contours — shown in the Graphics tab |
| `run_parametric_sweep` | Batch runs with summary curves and peak values |
| `list_runs` | Rediscover ids from an earlier session |
| `check_environment` | Report the detected toolchain |
| `save_project` / `load_project` / `list_saved_projects` | Exchange complete setups with the GUI |
| `get_settings` / `update_settings` | Read and change persistent preferences |

Tools return structured errors rather than raising, so an agent that passes an
out-of-range angle of attack gets a message it can act on.

### The built-in assistant

`backend/ai_agent.py` drives the same fifteen tools from inside the program
over OpenRouter's OpenAI-compatible API, so no external MCP client is needed.
Schemas are read from `server.list_tools()` rather than hand-copied, and a
test asserts the direct-call registry and the registered tool list are
identical — a tool cannot exist on one surface and be missing from the other.

The tool list *is* the boundary: no shell, no arbitrary file access, no code
execution. Meshing, solving and sweeping are confirmed with the operator
before they start, and a round cap bounds what one confused request can spend.

The transcript is written as the work happens: each round, the model's own
commentary before a tool call (which was previously received and discarded),
the call with its arguments, and the outcome with its duration. A request that
spends twenty minutes calling tools now leaves a record of what it did instead
of a blank panel. Transport failures — a reset connection, a rate limit, a 502
— are retried with backoff and the retry is written into that record; a
rejected key is not retried, because it will not improve.

Spending is shown as it happens rather than totalled afterwards — tokens,
generation rate and OpenRouter's own cost figure, updated after every round,
so a request that is going to be expensive is visible while there is still
time to stop it. The rate is measured against time spent waiting on the
model, not wall time: a request that spends four minutes meshing has not
slowed the model down. Where a provider reports no price the field stays
blank instead of showing a zero, because an invented figure is worse than
none.

The API key goes to the OS credential manager where one exists and to an
owner-only file where none does — never into a `.atsproj` project or
`settings.json`, both of which are plain JSON meant for version control. When
the fallback is used, the interface says so rather than implying a security it
does not have.

HTTP sits behind a `ChatClient` interface, so the whole agent loop — tool
dispatch, multi-step reasoning, malformed arguments, declined calls, iteration
limits — is tested with no network and no key, the same arrangement `FakeRunner`
gives the solver.

Register with an MCP client:

```json
{
  "mcpServers": {
    "aerothermalstudio": {
      "command": "python",
      "args": ["C:/path/to/CFD-Program/run_app.py", "--mcp"]
    }
  }
}
```

---

## Requirements

- Windows 11 (the analysis, meshing and rendering stack is cross-platform;
  SU2 and MS-MPI are the Windows-specific parts)
- Python 3.11+
- Microsoft MPI v10.1.3 or newer
- SU2 8.x

Tuned for a 6-core / 12-thread CPU with 16 GB RAM: 10 MPI ranks by default,
leaving two threads for the interface, and sequential sweep execution because
16 GB will not hold two 750k-cell RANS solutions at once.

`setup_env.py` refuses to install an SU2 archive with no recorded SHA-256 and
prints manual instructions instead. Silently installing an unverified binary
that will then execute on your machine is not a convenience worth having.

---

## Testing

```bash
pytest -q                 # full suite
pytest -q -m "not slow"   # skip the end-to-end meshing cases
```

Gmsh, NumPy, SciPy, PyVista and Qt are exercised for real. SU2 is replaced by
`FakeRunner`, which replays recorded solver output, so the config, parsing,
convergence and force-reduction layers are all covered on a machine with no
solver installed — and the real subprocess path is tested with small scripts,
so streaming, cancellation and timeouts are genuinely verified too, including
a child that goes silent and one that spawns a grandchild holding the output
pipe.

Physics is checked against closed-form results rather than golden numbers: ISA
values against the published tables, exact y⁺ sizing round trips, a force
through a hinge line producing zero hinge torque, a normal force located
exactly at its application station, and the roof energy balance closing at the
solved temperature.

---

## Known limitations

- The conjugate track's multizone mesh generation reuses the aerodynamic
  pipeline with additional markers; the solid-zone meshing path has not been
  exercised against a real SU2 CHT run here.
- The radiative boundary condition is linearised between outer iterations,
  which is an approximation to a fully coupled radiation solve.
- Fin/airframe face classification uses a radial-extent heuristic. It is
  reported in the mesh output so it can be checked, and can be overridden.
- No SU2 solve has been executed in this environment; the solver layer is
  verified against recorded output, and the first real solve should be run on
  the target machine.
