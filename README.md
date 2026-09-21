# AeroThermalStudio

A native Windows 11 CFD and thermal simulation platform for two jobs:

1. **Rocket and fin aerodynamics** — drag, lift, centre of pressure and fin
   hinge torque from Mach 0.2 to 3.5, at arbitrary angle of attack and
   sideslip.
2. **Sensor microclimate analysis** — what a BMP580 actually reads inside a
   3D-printed enclosure with a ram-air sampling tube, mounted above a
   solar-irradiated surface such as a tram roof.

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

## Install, update and run from `cmd`

The whole thing from an empty Command Prompt on Windows 11. You need
[Python 3.11+](https://www.python.org/downloads/) (tick **Add python.exe to
PATH** in the installer) and [Git](https://git-scm.com/download/win).

### Install

```cmd
cd %USERPROFILE%
git clone https://github.com/Tomasraketak/CFD-Program.git
cd CFD-Program

py -3.11 -m venv .venv
.venv\Scripts\activate

python setup_env.py
```

The virtual environment is optional but worth having: it keeps these packages
out of your system Python, and the `.bat` launchers find `.venv` on their own,
so after this you can double-click them without activating anything.

`setup_env.py` checks the machine, installs the Python packages, and offers to
fetch SU2. It **refuses to install an SU2 archive with no recorded SHA-256**
and prints manual instructions instead — silently installing an unverified
binary that will then execute on your machine is not a convenience worth
having. Microsoft MPI is a separate download it will point you at.

Confirm it works:

```cmd
python run_app.py --check
python run_app.py --demo
```

`--check` lists what was found; `--demo` meshes a sample rocket end to end and
needs no solver. If `--demo` produces a `.su2` file, the install is sound even
when SU2 itself is still missing.

### Run

```cmd
cd %USERPROFILE%\CFD-Program
.venv\Scripts\activate

python run_app.py --gui
```

Or just double-click `AeroThermalStudio.bat`, which does both lines for you.

| Command | What it does |
|---|---|
| `python run_app.py --gui` | Desktop application (the default with no flag) |
| `python run_app.py --check` | Report the detected toolchain |
| `python run_app.py --check --json` | Same, machine-readable |
| `python run_app.py --demo` | Mesh the sample rocket, no solver needed |
| `python run_app.py --mcp` | MCP server on stdio, for an external AI client |
| `pytest -q` | Run the test suite |

### Update

```cmd
cd %USERPROFILE%\CFD-Program
.venv\Scripts\activate

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
| `'python' is not recognized` | Python is not on PATH. Use `py` instead of `python`, or reinstall with **Add python.exe to PATH** ticked |
| `'git' is not recognized` | Install Git for Windows, then open a *new* Command Prompt |
| `.venv\Scripts\activate` does nothing visible | It worked — the prompt gains a `(.venv)` prefix |
| `running scripts is disabled` | That is PowerShell. Use `cmd`, or `.venv\Scripts\activate.bat` |
| `--check` reports no SU2 or MPI | The GUI, meshing and the analytical sensor model still work; only the CFD solve needs them |

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

```
core/       models  store  units  atmosphere  platform_env
            project  settings  credentials
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

---

## Aerodynamics

The convective scheme follows the regime: JST central differencing below
Mach 0.8, Roe upwind with MUSCL reconstruction and the Venkatakrishnan
limiter above it (plus an entropy fix so Roe cannot admit expansion shocks),
with SST k-ω throughout and wall functions matching the y⁺ 30–60 mesh.

**Hinge torque** is computed here rather than taken from SU2, because a fin
hinge is an arbitrary line the solver knows nothing about. The aerodynamic
moment is transferred to the hinge point with the r × F parallel-axis
correction and projected onto the hinge axis, so a force acting through the
hinge line yields exactly zero torque regardless of where the solver's moment
origin sits.

Convergence stops on *either* the residual threshold or steady forces — a
RANS solve often reaches usable forces well before the residual target, and
the forces are what the operator wants.

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
| `generate_cfd_visualization` | Surface pressure, Mach slice, streamlines, thermal contours |
| `run_parametric_sweep` | Batch runs with summary curves and peak values |
| `list_runs` | Rediscover ids from an earlier session |
| `check_environment` | Report the detected toolchain |
| `save_project` / `load_project` / `list_saved_projects` | Exchange complete setups with the GUI |
| `get_settings` / `update_settings` | Read and change persistent preferences |

Tools return structured errors rather than raising, so an agent that passes an
out-of-range angle of attack gets a message it can act on.

### The built-in assistant

`backend/ai_agent.py` drives the same twelve tools from inside the program
over OpenRouter's OpenAI-compatible API, so no external MCP client is needed.
Schemas are read from `server.list_tools()` rather than hand-copied, and a
test asserts the direct-call registry and the registered tool list are
identical — a tool cannot exist on one surface and be missing from the other.

The tool list *is* the boundary: no shell, no arbitrary file access, no code
execution. Meshing, solving and sweeping are confirmed with the operator
before they start, and a round cap bounds what one confused request can spend.

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
so streaming, cancellation and timeouts are genuinely verified too.

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
