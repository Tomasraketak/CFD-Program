# AeroThermalStudio — MCP and AI Guide

How to drive the platform from an AI assistant, and the complete tool
reference.

**Contents**

1. [What this is](#1-what-this-is)
2. [Setup](#2-setup)
3. [Tool reference](#3-tool-reference)
4. [Worked workflows](#4-worked-workflows)
5. [Writing good prompts](#5-writing-good-prompts)
6. [Error handling](#6-error-handling)
7. [Notes for agent authors](#7-notes-for-agent-authors)
8. [Limits and judgement](#8-limits-and-judgement)
9. [The built-in assistant](#9-the-built-in-assistant)

---

## 1. What this is

The Model Context Protocol (MCP) is a standard way for AI assistants to call
external tools. AeroThermalStudio ships an MCP server exposing **fourteen
tools** covering everything the desktop program can do: CAD import, meshing,
aerodynamic and thermal simulation, rendering, parametric sweeps, and project
and settings management.

An assistant connected to it can be asked, in ordinary language:

> "Mesh `C:\models\rocket.step` with the nose along +X, run it at Mach 2 and
> 5° angle of attack, and tell me the drag and the hinge torque on a fin at
> [0.9, 0.04, 0] rotating about [0, 1, 0]."

and it will carry out the whole sequence.

### Why the GUI and the agent agree

Both are thin clients over the same typed parameter core. The GUI builds its
forms from the parameter models; the MCP server generates its tool schemas
from the same models. A field's bounds, units and description are written once
and appear in both.

The practical consequence: an agent cannot do anything a human could not, and
cannot produce a setup the GUI would reject. Both also share one run registry,
so a mesh an agent produced opens in the GUI, and a project a human saved can
be loaded by an agent.

---

## 2. Setup

### Claude Desktop

Edit the MCP configuration file:

- **Windows** — `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS** — `~/Library/Application Support/Claude/claude_desktop_config.json`

Add:

```json
{
  "mcpServers": {
    "aerothermalstudio": {
      "command": "C:\\AeroThermalStudio\\Run-MCP-Server.bat"
    }
  }
}
```

Or invoke Python directly:

```json
{
  "mcpServers": {
    "aerothermalstudio": {
      "command": "python",
      "args": ["C:\\AeroThermalStudio\\run_app.py", "--mcp"]
    }
  }
}
```

Restart the client. The tools should appear in its tool list.

### Other clients

Any MCP client works. The server speaks JSON-RPC over stdin/stdout; start it
with `python run_app.py --mcp`.

### Verify

Ask the assistant to call `check_environment`. You should get back the
detected toolchain:

```json
{
  "ok": true,
  "os_name": "Windows",
  "cpu_count": 12,
  "recommended_ranks": 10,
  "mpi_launcher": "C:\\Program Files\\Microsoft MPI\\Bin\\mpiexec.exe",
  "su2_cfd": "C:\\Users\\you\\AppData\\Local\\AeroThermalStudio\\su2\\bin\\SU2_CFD.exe",
  "solver_ready": true
}
```

If `solver_ready` is `false`, the `missing` list says what to install.
Meshing, the analytical thermal model and rendering still work.

### A note on the console window

`Run-MCP-Server.bat` opens a window that looks idle. That is correct — the
server is talking JSON-RPC on its standard streams, not printing. Do not close
it while the assistant is working.

---

## 3. Tool reference

All fourteen tools return a JSON object with an `ok` field. On failure,
`ok: false` and `error` carries a message intended to be acted on.

### 3.1 `set_geometry_and_mesh`

Import CAD, align it, build the domain, generate the mesh.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `step_file_path` | string | `""` | Omit to mesh whatever is loaded in the interface |
| `nose_direction` | string | read from the shape | Nose **to tail**: `+X`, `-X`, `+Y`, `-Y`, `+Z`, `-Z` |
| `nose_vector` | list[3] | null | Arbitrary direction, same sense; overrides `nose_direction` |
| `reference_origin` | list[3] | `[0,0,0]` | Point moved to the tunnel origin |
| `domain_multipliers` | object | see notes | `{"upstream": 5, "downstream": 10, "radial": 5}` |
| `domain_shape` | string | `"cylinder"` | `cylinder` or `box` |
| `mesh_resolution` | string | `"medium"` | `coarse`, `medium`, `fine` |
| `track` | string | `"aerodynamic"` | `aerodynamic` or `thermal` |
| `boundary_layers` | int | 7 | 5–8 |
| `target_yplus` | float | 45.0 | 30–300 |
| `scale_to_meters` | float | read from the file | `0.001` for millimetres |
| `sizing_mach` | float | 1.0 | Condition the boundary layer is sized for |
| `body_kind` | string | read from the shape | `rocket` (longest side over 3.5× the others) or `fin` (thin plate; chord and planform reference) |
| `pitch_axis` | string | null | CAD axis the model tilts about for an angle of attack, e.g. `+Z`; must be across the flow |
| `sizing_altitude_m` | float | 0.0 | |
| `max_targeting_iterations` | int | 4 | Remesh attempts to hit the cell band |

Returns `mesh_id`, `cell_count`, `within_target_band`, `reference_length_m`,
`reference_diameter_m`, `reference_area_m2`, `estimated_yplus`,
`boundary_markers`, `min_quality`, `poor_prism_count`, `wall_time_s`, plus `step_file_path`,
`scale_to_meters`, `nose_direction` and the notes saying how the last two
were arrived at.

Takes tens of seconds to a few minutes.

**On `nose_direction`.** It is the axis the body runs along *from the nose
towards the tail*, because that direction is rotated onto +X, which places
the nose at the upstream end of the tunnel. A rocket drawn standing up, tip
at the top, is `-Y`. Leave the parameter out and the geometry answers: the
longest axis is the body axis, and the end that tapers is the nose. When
both ends are alike the call fails rather than guessing:

```
set_geometry_and_mesh(step_file_path = "C:\\models\\tube.step")
   └─ ok = false
      error   = "which end is the nose? the body lies along X, but both ends
                 are about equally thick (90 and 90) ..."
      needs   = ["nose_direction"]
      axis    = "X"
```

Put that question to the operator. Do not pick a side: a rocket meshed
backwards returns a complete, plausible drag polar for a vehicle flying
tail-first, and nothing downstream objects.

### 3.2 `run_aerodynamic_simulation`

Run one RANS simulation point.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `mesh_id` | string | — | **Required.** From `set_geometry_and_mesh` |
| `velocity_type` | string | `"mach"` | `mach` or `tas` |
| `velocity_val` | float | 2.0 | Mach 0.05–3.5, or m/s |
| `aoa_deg` | float | 0.0 | ±20 |
| `sideslip_deg` | float | 0.0 | ±20 |
| `altitude_m` | float | 0.0 | −610 to 32 000 |
| `hinge_axes` | list[object] | `[]` | `{"name", "point": [x,y,z], "direction": [u,v,w], "frame": "rocket"}` — rocket axes unless `frame` is `"solver"` |
| `reference_area_m2` | float | null | Overrides the measured value |
| `reference_length_m` | float | null | Overrides the measured value |
| `moment_origin` | list[3] | null | Overrides the default |
| `mpi_ranks` | int | 10 | |
| `max_iterations` | int | 5000 | |
| `convergence_residual` | float | −5.0 | log₁₀ RMS density |
| `turbulence_model` | string | `"SST"` | `SST` or `SA` |
| `cfl_number` | float | null | Starting CFL; from the regime when null: 5.0 below Mach 0.6, 2.0 up to 1.2, 1.0 above |
| `cfl_growth` | float | null | Adaptive CFL growth per iteration, 1.0–3.0; regime default 1.15 / 1.10 / 1.05 |
| `cfl_max` | float | null | Adaptive CFL ceiling; regime default 100 / 50 / 25 |
| `convective_scheme` | string | null | `JST`, `ROE`, `AUSM`, `HLLC`; null chooses JST below Mach 0.8, Roe above |

Returns `sim_id`, `mach`, `axis_convention`, `forces_rocket_frame_n`
(`fx`/`fy`/`fz`), `forces_solver_frame_n`, `drag_n`, `lift_n`,
`sideforce_n`, `coefficients` (`cd`/`cl`/`cs`/`cm_pitch`),
`center_of_pressure_rocket_frame`, `center_of_pressure_solver_frame`,
`hinge_torques`, `iterations`, `converged`, and `rescued` with `notes`.

**Two frames.** The *rocket frame* is the one the operator sees: nose along
+Z, origin at the reference origin. Drag on a rocket flying nose-first is a
negative `fz`; the lift from a positive angle of attack is `+fx`; sideslip
produces `fy`. The *solver frame* is the mesh's own — body along +X, flow
along +X — and is included for reference. They differ by a fixed rotation:
x_rocket = z_solver, y_rocket = y_solver, z_rocket = −x_solver. Quote the
rocket frame to people. Hinge points and directions passed in are read in the
rocket frame too, unless an axis says `"frame": "solver"`.

A supersonic cold start can blow up in a few iterations. That is now caught
at once and retried automatically at first order, then restarted at second
order from the result. When that happened `rescued` is true and `notes`
explains it — say so in your answer rather than presenting the number as an
ordinary one, because a case that needed rescuing is usually telling the
operator their mesh is coarse for the flow condition.

Leave `cfl_number` null unless the operator asks for a specific value.
Pinning 5.0 at Mach 1.3 is what made the solve diverge at iteration six. A
low `cfl_number` alone does not make a cautious run: the adaptive ramp
decides where the CFL goes, so lower `cfl_max` and keep `cfl_growth` near
1.05. If even the rescue diverges, a second remesh is rarely the answer;
check the mesh's `min_quality`, then try `convective_scheme: "ROE"` with a
low `cfl_max`.

Takes 3–8 minutes on the reference machine. Requires SU2.

### 3.3 `run_sensor_thermal_simulation`

Predict the BMP580 reading and its bias.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `enclosure_step` | string | `""` | CAD path, recorded with the run |
| `mesh_id` | string | null | Omit for the analytical answer |
| `vehicle_speed_ms` | float | 2.0 | 0–50 |
| `ambient_temp_c` | float | 25.0 | −50 to 70 |
| `solar_flux_w_m2` | float | 800.0 | 0–1400 |
| `height_above_roof_m` | float | 0.1 | 0.01–2 |
| `sensor_xyz` | list[3] | `[0.03,0.02,0.015]` | Die coordinate |
| `housing_solar_absorptivity` | float | 0.30 | |
| `housing_emissivity` | float | 0.90 | |
| `roof_solar_absorptivity` | float | 0.65 | |
| `roof_emissivity` | float | 0.85 | |
| `housing_conductivity_w_mk` | float | 0.18 | |
| `sensor_power_w` | float | 0.001 | |
| `analytic_only` | bool | false | Skip CFD |
| `mpi_ranks` | int | 10 | |

Returns `method`, `sensor_temp_c`, `ambient_temp_c`, `delta_t_error_k`,
`roof_temp_c`, `housing_temp_c`, `tube_mass_flow_kg_s`,
`roof_thermal_boundary_layer_m`, `intake_in_roof_plume`,
`self_heating_rise_k`, `wall_coupling_rise_k`.

**With `analytic_only: true` this returns in milliseconds and needs no mesh or
solver.** That makes it ideal for exploration — an agent can evaluate dozens
of design variants in a single turn.

### 3.4 `generate_cfd_visualization`

Render an image from a completed simulation.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `sim_id` | string | — | **Required**. A sweep point as the sweep lists it (`<sweep_id>-003`) works too: every point keeps its solution |
| `visualization_type` | string | `"surface_pressure"` | `surface_pressure`, `mach_slice`, `schlieren`, `streamlines`, `thermal` |
| `camera_view` | string | `"isometric"` | `isometric`, `front`, `back`, `side`, `top`, `bottom`, `nose_quarter`, `tail_quarter` |
| `slice_normal` | list[3] | `[0,1,0]` | Cutting-plane normal |
| `colormap` | string | `"turbo"` | `turbo`, `coolwarm`, `viridis`, `jet`, `plasma`, `inferno` |
| `resolution` | string | `"4k"` | `preview`, `hd`, `2k`, `4k` |
| `output_path` | string | null | Defaults into the run folder; give a path to export elsewhere |
| `frame` | string | null | `rocket` (nose along +Z) or `solver`; null means `rocket` for aerodynamic runs, `solver` for thermal |

Returns `image_path`, `frame` and, in the rocket frame, `axis_convention`.

Images written into the run folder appear in the program's **Graphics** tab,
where the operator can view and export them; one the in-app assistant renders
also appears inline in its conversation. Each carries a caption naming the
mode, Mach number, angle of attack and run. A Mach slice is cropped to the
rocket plus three quarters of a body length around it; use
`camera_view: "side"` to look straight at it.

### 3.5 `run_parametric_sweep`

Batch simulations across a swept parameter, reusing one mesh.

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `mesh_id` | string | — | **Required** |
| `param_name` | string | `"mach"` | `mach`, `aoa`, `sideslip`, `altitude` |
| `values` | list[float] | — | **Required** |
| `fixed_params` | object | `{}` | Conditions held constant |
| `hinge_axes` | list[object] | `[]` | As above |
| `mpi_ranks` | int | 10 | |
| `stop_on_error` | bool | false | Abort at the first failure |

Returns `sweep_id`, `points`, `curves`, `extremes` (including
`servo_sizing_torque_nm`), `succeeded`, `failed`.

Points run sequentially. **Total time is the point count times the per-point
time** — a six-point sweep is half an hour. Warn the user before starting one.

### 3.6 `list_runs`

List stored meshes, simulations and sweeps, newest first.

| Parameter | Type | Default |
|---|---|---|
| `kind` | string | null (`mesh`, `aero`, `thermal`, `sweep`) |
| `limit` | int | 25 |

Use this to recover a `mesh_id` from an earlier session.

### 3.7 `check_environment`

Report the detected toolchain. Call this first when a simulation reports a
missing solver.

### 3.8 `get_active_geometry`

Report — or set — the CAD file the operator has loaded in the desktop
application.

| Parameter | Type | Default |
|---|---|---|
| `step_file_path` | string | null (report only; given, it records that file) |
| `nose_direction` | string | null |
| `scale_to_meters` | float | null (read from the file) |

The interface records every STEP file opened in it, together with the length
unit read out of the file and the model's overall size. Call this before
asking an operator for a path: when something is loaded,
`set_geometry_and_mesh` meshes it with no path at all.

```
get_active_geometry()
   └─ loaded = true
      geometry.step_file_path = "C:\\Users\\...\\Sapphire.step"
      geometry.units = "millimetres"
      geometry.largest_extent_m = 1.32
      geometry.scale_is_confident = true
```

When `scale_is_confident` is false, say so before spending solver time: the
file declared no unit, or declared one that does not match its own size.

### 3.9 `save_project`

Write a complete setup to a `.atsproj` file the GUI can open.

Takes the same parameters as the meshing and simulation tools, plus `name`,
`path`, `description`, `author`, `notes`, `sweep_parameter`, `sweep_values`,
`thermal` (an object) and `mesh_id`.

### 3.10 `load_project`

Read a project file. Takes `path`; returns every section plus `last_mesh_id`.

### 3.11 `list_saved_projects`

List available projects with one-line summaries. Takes an optional
`directory`.

### 3.12 `get_settings` / 3.13 `update_settings`

Read and modify persistent preferences: `default_mpi_ranks`,
`default_colormap`, `default_render_resolution`, `default_mesh_resolution`,
and the behaviour toggles. `update_settings` changes only the fields supplied.

### 3.14 `preview_orientation`

Draws a quick, low-resolution picture of the model (side and angled view)
with the oncoming air as blue arrows and the tilt axis as an orange rod with
a curved arrow — exactly as a mesh or solve with the same settings would use
them. Takes the geometry arguments of `set_geometry_and_mesh` (or a
`mesh_id`) plus `aoa_deg` and `sideslip_deg`, and returns `image_path`.

In the built-in assistant this is required: a setup the operator has not
seen — new file, nose direction, body kind, tilt axis, or a new angle — is
refused by `set_geometry_and_mesh`, `run_aerodynamic_simulation` and
`run_parametric_sweep` until it has been previewed **and the operator has
replied**. The assistant ends its turn after a preview; the operator's next
message counts as the answer, and a corrected setup is previewed again.
External MCP clients are not gated.

---

## 4. Worked workflows

### 4.1 Single simulation

```
check_environment
   └─ confirm solver_ready

set_geometry_and_mesh(
     step_file_path = "C:\\models\\rocket.step",
     nose_direction = "+X",
     mesh_resolution = "coarse")
   └─ mesh_id = "mesh-20260921-101500"
      cell_count = 252,303, estimated_yplus = 45.0

run_aerodynamic_simulation(
     mesh_id = mesh_id,
     velocity_val = 2.0,
     aoa_deg = 5.0,
     hinge_axes = [{"name": "fin_1",
                    "point": [0.9, 0.04, 0.0],
                    "direction": [0, 1, 0]}])
   └─ sim_id, cd = 0.42, cl = 0.25, hinge torque = 0.084 N m

generate_cfd_visualization(
     sim_id = sim_id,
     visualization_type = "mach_slice",
     camera_view = "side")
   └─ image_path
```

### 4.2 Sizing a servo

```
set_geometry_and_mesh(...)              once

run_parametric_sweep(                   find the worst Mach
     mesh_id = mesh_id,
     param_name = "mach",
     values = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
     fixed_params = {"aoa_deg": 10},
     hinge_axes = [...])
   └─ extremes.servo_sizing_torque_nm

run_parametric_sweep(                   refine at that Mach
     mesh_id = mesh_id,
     param_name = "aoa",
     values = [0, 5, 10, 15, 20],
     fixed_params = {"velocity_val": <worst Mach>},
     hinge_axes = [...])
   └─ the true peak
```

Report the peak with a recommended margin, and say which condition produced
it.

### 4.3 Sensor design exploration

The analytical mode is fast enough to explore a design space in one turn:

```
run_sensor_thermal_simulation(analytic_only = true,
                              housing_solar_absorptivity = 0.1)
run_sensor_thermal_simulation(analytic_only = true,
                              housing_solar_absorptivity = 0.3)
run_sensor_thermal_simulation(analytic_only = true,
                              housing_solar_absorptivity = 0.9)
run_sensor_thermal_simulation(analytic_only = true,
                              height_above_roof_m = 0.05)
run_sensor_thermal_simulation(analytic_only = true,
                              vehicle_speed_ms = 0.0)
```

Then present the trade-off. Always include the **stationary** case: a tram at
a stop in full sun is the worst case and the one users forget.

### 4.4 Handing work to a human

```
save_project(
     name = "Fin sizing study",
     description = "Mach sweep for servo selection",
     notes = "Peak torque 0.084 N m at Mach 2.5, 10 deg. Recommend a 0.2 N m servo.",
     step_file_path = "C:\\models\\rocket.step",
     hinge_axes = [...],
     sweep_parameter = "mach",
     sweep_values = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0],
     mesh_id = mesh_id)
```

Tell the user the path. They open it with **File → Open project** and see
exactly the setup you used.

---

## 5. Writing good prompts

**Be specific about geometry and orientation.** The nose direction is the
setting that most often goes wrong, and a wrong answer looks plausible.

> "Mesh `C:\models\rocket.step`. The nose points along **+Z** in the CAD file
> and the model is in **millimetres**."

**Say what you want to learn, not just what to compute.**

> "I need to choose a servo for the pitch fins. Find the worst-case hinge
> torque across the flight envelope: Mach 0.5 to 3, up to 15° angle of
> attack."

**Give the physical context for the sensor track.**

> "A BMP580 in a white PETG housing, 15 cm above a tram roof, sampling tube
> facing forward. Summer noon, tram at 5 m/s but often stopped. How far off
> will it read, and what should I change?"

**Ask for the reasoning.**

> "Show me the drag polar and tell me where the drag rise starts."

---

## 6. Error handling

Tools return structured errors rather than raising. Common ones:

| Error contains | Meaning | Fix |
|---|---|---|
| `SU2_CFD was not found` | Solver not installed | `check_environment`, then install |
| `needs an MPI launcher` | MPI missing for a parallel run | Install MS-MPI, or set `mpi_ranks: 1` |
| `invalid parameters` | A value outside its bounds | Read the message; it names the field |
| `no record` / `not a mesh` | Bad `mesh_id` | `list_runs` to find a valid one |
| `STEP file not found` | Bad path | Check it; Windows paths need escaped backslashes in JSON |
| `no solid volumes` | Surface-only CAD | The model must be sewn into a solid |
| `not a closed manifold` | Sharp CAD feature | Add a small radius to knife-edges and points |
| `mesh file not found` | Mesh deleted | Regenerate it |

A good agent reads the message and acts on it rather than repeating the call.

---

## 7. Notes for agent authors

**Runtimes vary enormously.**

| Operation | Typical |
|---|---|
| `run_sensor_thermal_simulation` with `analytic_only` | milliseconds |
| `check_environment`, `list_runs`, project tools | milliseconds |
| `generate_cfd_visualization` | seconds |
| `set_geometry_and_mesh` | 30 s – 3 min |
| `run_aerodynamic_simulation` | 3–8 min |
| `run_parametric_sweep` | points × 3–8 min |

Tell the user before starting anything long. A six-point sweep is half an
hour; starting one without warning is a poor experience.

**Reuse the mesh.** Meshing is expensive and does not depend on the flight
condition — angle of attack and Mach are applied by the solver. One mesh
serves an entire sweep. Only regenerate when the geometry, domain or mesh
settings change.

**Start coarse.** Use `coarse` resolution to validate a setup, then refine.
Discovering a wrong nose direction after a `fine` mesh wastes minutes.

**Check `within_target_band`.** If false, the mesher could not reach the
requested cell count. The mesh is usable but the runtime estimate will be off.

**Check `converged`.** An unconverged result should be reported as such, not
presented as fact.

**Don't invent a centre of pressure.** `NaN` at zero incidence is correct and
meaningful — there is genuinely no defined centre of pressure without a
transverse force. Suggest running at 2–5° instead.

**Verify before trusting.** A single CFD run is not a validated answer.
Encourage a mesh independence check for anything consequential, and say so
plainly when a result has not been verified.

---

## 8. Limits and judgement

**Validity envelope.** Mach 0.05–3.5, angles within ±20°, altitude −610 to
32 000 m. Beyond ±20° a slender body separates massively and a steady RANS
solution stops being meaningful — the number would still appear, and it would
be wrong.

**The thermal conjugate path is less exercised** than the analytical model.
The solid-zone meshing route has not been run against a real SU2 multizone
solve. The analytical model is well tested and its energy balances are
verified to close.

**Radiation is linearised** between outer iterations rather than fully
coupled.

**Fin classification is a heuristic** based on radial extent. It is reported
in the mesh output so it can be checked.

**CFD is a model.** It gives precise answers to the question posed, which is
not always the question asked. Turbulence models are correlations; mesh
resolution changes answers; boundary conditions embody assumptions. Present
results with that context rather than as measurements.

---

## 9. The built-in assistant

The same fourteen tools also back an assistant **inside the program**, on the
**AI Assistant** tab. It is for an operator who wants to type a request and
have it carried out without running an external MCP client at all.

### How it differs from an MCP client

| | MCP server | Built-in assistant |
|---|---|---|
| Started by | An external client (Claude Desktop, an agent framework) | The AI Assistant tab |
| Transport | stdio, MCP protocol | OpenRouter's OpenAI-compatible HTTP API |
| Model | Whatever the client runs | Any OpenRouter model id, chosen in the tab |
| Credentials | The client's own | An OpenRouter key, stored by the program |
| Tool schemas | `server.list_tools()` | The same call, converted to OpenAI function shape |

Both surfaces dispatch into the same functions: `mcp_server.TOOL_FUNCTIONS`
maps each registered tool name to the function the MCP handler calls, and
`mcp_server.call_tool(name, arguments)` invokes one directly, returning the
same structured `{"ok": ...}` result rather than raising. A test asserts the
registry and the registered tool list are identical, so a tool cannot be
offered on one surface and missing from the other.

### The agent loop

`backend.ai_agent.AIAssistant.ask()` runs the standard tool-calling loop: send
the conversation with the tool schemas, execute any tool calls the model
returns, append each result as a `tool` message, repeat until the model
answers in text or the round cap is reached. Transport sits behind the
`ChatClient` interface, so `FakeChatClient` replays scripted conversations and
the whole loop is tested without a network or a key.

Two behaviours are deliberate. `mcp_server.LONG_RUNNING_TOOLS` names the three
tools that occupy the machine for minutes to hours; an approval callback is
consulted before each of them, and a declined call is reported back to the
model as a refusal it must not retry unchanged. The round cap
(`ai_max_tool_rounds`, default 12) bounds what one request can spend. After a
successful `preview_orientation` the loop makes one more model call so it can
ask about the picture, then ends the turn and waits for the operator.

### Its system prompt

The assistant is instructed on the things a model otherwise gets wrong here:
reuse one `mesh_id` across simulations because meshing does not depend on the
flight condition; state what a step will cost before starting it; report
`converged: false` and `within_target_band: false` rather than presenting a
number as settled; treat a NaN centre of pressure at zero angle of attack as
correct and suggest 2–5° instead; and say plainly that it cannot run shell
commands or read arbitrary files.

---

## See also

- **[TUTORIAL.md](TUTORIAL.md)** — step-by-step introduction
- **[USER_GUIDE.md](USER_GUIDE.md)** — reference for every setting
- **[../../README.md](../../README.md)** — architecture and implementation
