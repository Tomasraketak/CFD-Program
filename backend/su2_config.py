"""Generation of SU2 configuration files.

Everything here is derived from the typed parameter models, so a setting has
one definition shared by the GUI, the MCP schema and the solver input.

The main piece of judgement is the flow-regime selector. A central scheme with
scalar dissipation (JST) is efficient and accurate in smooth subsonic flow but
oscillates across a shock; an upwind scheme with a limiter captures shocks
cleanly but is more dissipative and slower to converge. The transition is made
at Mach 0.8, where transonic pockets first appear on a slender body.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.mesh_pipeline import (
    MARKER_FARFIELD,
    MARKER_SYMMETRY,
    MARKER_WALL_FINS,
    MARKER_WALL_ROCKET,
)
from core.models import (
    AeroRunRequest,
    ConvectiveScheme,
    FlowParams,
    ReferenceValues,
    SolverParams,
    TurbulenceModel,
)
from core.units import MACH_SUBSONIC_MAX

# Wall-function treatment matching the y+ 30-60 band the mesher targets.
WALL_FUNCTION = "STANDARD_WALL_FUNCTION"

# Venkatakrishnan limiter coefficient. Smaller values limit more aggressively;
# 0.05 is the usual compromise between shock capture and convergence.
VENKAT_LIMITER_COEFF = 0.05


@dataclass
class ConfigContext:
    """Resolved inputs a configuration is written from."""

    mesh_filename: str
    reference_area_m2: float
    reference_length_m: float
    moment_origin: tuple[float, float, float]
    wall_markers: tuple[str, ...]
    has_symmetry: bool = False
    restart_filename: str | None = None


def select_convective_scheme(
    mach: float, override: ConvectiveScheme | None = None
) -> ConvectiveScheme:
    """Choose the convective flux scheme for a Mach number.

    JST central differencing below Mach 0.8; Roe upwind at and above it, where
    shocks appear and a central scheme would ring.
    """
    if override is not None:
        return override
    return (
        ConvectiveScheme.JST if mach < MACH_SUBSONIC_MAX else ConvectiveScheme.ROE
    )


def uses_upwind(scheme: ConvectiveScheme) -> bool:
    """True for schemes that take MUSCL reconstruction and a limiter."""
    return scheme is not ConvectiveScheme.JST


def build_aero_config(
    flow: FlowParams,
    solver: SolverParams,
    context: ConfigContext,
) -> str:
    """Write the SU2 configuration text for an aerodynamic run.

    Parameters
    ----------
    flow:
        Freestream conditions and body attitude.
    solver:
        Numerics and convergence control.
    context:
        Mesh filename, reference quantities and marker names.

    Returns
    -------
    str
        Complete configuration file contents.
    """
    state = flow.atmosphere()
    mach = flow.mach()
    scheme = select_convective_scheme(mach, solver.convective_scheme)
    upwind = uses_upwind(scheme)

    lines: list[str] = []
    add = lines.append

    add(_banner("AeroThermalStudio - aerodynamic run"))
    add(f"% Mach {mach:.4f}, alpha {flow.aoa_deg:g} deg, beta {flow.sideslip_deg:g} deg")
    add(f"% Freestream {state.pressure_pa:.1f} Pa, {state.temperature_k:.2f} K")
    add("")

    add(_banner("Problem definition"))
    add("SOLVER= RANS")
    add(f"KIND_TURB_MODEL= {solver.turbulence_model.value}")
    if solver.turbulence_model is TurbulenceModel.SST:
        add("SST_OPTIONS= V1994m")
    add("MATH_PROBLEM= DIRECT")
    add(f"RESTART_SOL= {_yes_no(solver.restart)}")
    add("SYSTEM_MEASUREMENTS= SI")
    add("")

    add(_banner("Freestream conditions"))
    add(f"MACH_NUMBER= {mach:.6f}")
    add(f"AOA= {flow.aoa_deg:.6f}")
    add(f"SIDESLIP_ANGLE= {flow.sideslip_deg:.6f}")
    add("FREESTREAM_OPTION= TEMPERATURE_FS")
    add(f"FREESTREAM_PRESSURE= {state.pressure_pa:.6f}")
    add(f"FREESTREAM_TEMPERATURE= {state.temperature_k:.6f}")
    add("REYNOLDS_LENGTH= {:.9f}".format(context.reference_length_m))
    add(
        "REYNOLDS_NUMBER= {:.6f}".format(
            state.density_kg_m3
            * flow.speed_ms()
            * context.reference_length_m
            / state.viscosity_pa_s
        )
    )
    add("FLUID_MODEL= IDEAL_GAS")
    add("GAMMA_VALUE= 1.4")
    add("GAS_CONSTANT= 287.058")
    add("VISCOSITY_MODEL= SUTHERLAND")
    add("")

    add(_banner("Reference values"))
    add(f"REF_AREA= {context.reference_area_m2:.9f}")
    add(f"REF_LENGTH= {context.reference_length_m:.9f}")
    add("REF_DIMENSIONALIZATION= DIMENSIONAL")
    origin_x, origin_y, origin_z = context.moment_origin
    add(f"REF_ORIGIN_MOMENT_X= {origin_x:.9f}")
    add(f"REF_ORIGIN_MOMENT_Y= {origin_y:.9f}")
    add(f"REF_ORIGIN_MOMENT_Z= {origin_z:.9f}")
    add("")

    add(_banner("Boundary conditions"))
    walls = ", ".join(f"{name}, 0.0" for name in context.wall_markers)
    add(f"MARKER_HEATFLUX= ( {walls} )")
    wall_functions = ", ".join(
        f"{name}, {WALL_FUNCTION}" for name in context.wall_markers
    )
    add(f"MARKER_WALL_FUNCTIONS= ( {wall_functions} )")
    add(f"MARKER_FAR= ( {MARKER_FARFIELD} )")
    if context.has_symmetry:
        add(f"MARKER_SYM= ( {MARKER_SYMMETRY} )")
    marker_list = ", ".join(context.wall_markers)
    add(f"MARKER_PLOTTING= ( {marker_list} )")
    add(f"MARKER_MONITORING= ( {marker_list} )")
    add("")

    add(_banner("Numerics"))
    add("NUM_METHOD_GRAD= WEIGHTED_LEAST_SQUARES")
    add(f"CONV_NUM_METHOD_FLOW= {scheme.value}")
    if upwind:
        add(f"MUSCL_FLOW= {_yes_no(solver.muscl)}")
        add(f"SLOPE_LIMITER_FLOW= {solver.limiter}")
        add(f"VENKAT_LIMITER_COEFF= {VENKAT_LIMITER_COEFF}")
        if scheme is ConvectiveScheme.ROE:
            # Entropy fix, needed to keep the Roe solver from admitting
            # expansion shocks at supersonic Mach numbers.
            add("ENTROPY_FIX_COEFF= 0.05")
    else:
        # JST's 2nd and 4th order dissipation coefficients.
        add("JST_SENSOR_COEFF= ( 0.5, 0.02 )")
    add("TIME_DISCRE_FLOW= EULER_IMPLICIT")

    add("CONV_NUM_METHOD_TURB= SCALAR_UPWIND")
    add("MUSCL_TURB= NO")
    add("SLOPE_LIMITER_TURB= VENKATAKRISHNAN")
    add("TIME_DISCRE_TURB= EULER_IMPLICIT")
    add("")

    add(_banner("Linear solver"))
    add("LINEAR_SOLVER= FGMRES")
    add("LINEAR_SOLVER_PREC= ILU")
    add("LINEAR_SOLVER_ILU_FILL_IN= 0")
    add("LINEAR_SOLVER_ERROR= 1E-6")
    add("LINEAR_SOLVER_ITER= 10")
    add("")

    add(_banner("Convergence"))
    add(f"CFL_NUMBER= {solver.cfl_number:g}")
    add(f"CFL_ADAPT= {_yes_no(solver.cfl_adapt)}")
    if solver.cfl_adapt:
        # (down factor, up factor, min CFL, max CFL)
        add(f"CFL_ADAPT_PARAM= ( 0.1, 2.0, {solver.cfl_number * 0.1:g}, 100.0 )")
    add(f"ITER= {solver.max_iterations}")
    add("CONV_FIELD= RMS_DENSITY")
    add(f"CONV_RESIDUAL_MINVAL= {solver.convergence_residual:g}")
    add("CONV_STARTITER= 10")
    add("")

    add(_banner("Input / output"))
    add(f"MESH_FILENAME= {context.mesh_filename}")
    add("MESH_FORMAT= SU2")
    add("TABULAR_FORMAT= CSV")
    add("CONV_FILENAME= history")
    add("VOLUME_FILENAME= flow")
    add("SURFACE_FILENAME= surface_flow")
    add("RESTART_FILENAME= restart_flow.dat")
    if context.restart_filename:
        add(f"SOLUTION_FILENAME= {context.restart_filename}")
    add("OUTPUT_FILES= ( RESTART, PARAVIEW_MULTIBLOCK, SURFACE_CSV )")
    add("WRT_VOLUME_OVERWRITE= YES")
    add("OUTPUT_WRT_FREQ= 250")
    add(
        "SCREEN_OUTPUT= ( INNER_ITER, RMS_DENSITY, RMS_ENERGY, LIFT, DRAG, "
        "SIDEFORCE, MOMENT_X, MOMENT_Y, MOMENT_Z )"
    )
    add(
        "HISTORY_OUTPUT= ( ITER, RMS_RES, AERO_COEFF, FORCES_BREAKDOWN )"
    )
    add("SCREEN_WRT_FREQ_INNER= 1")
    add("")

    return "\n".join(lines) + "\n"


def build_config_for_request(
    request: AeroRunRequest,
    mesh_filename: str,
    reference: ReferenceValues,
    measured_area_m2: float,
    measured_length_m: float,
    wall_markers: tuple[str, ...] = (MARKER_WALL_ROCKET, MARKER_WALL_FINS),
    has_symmetry: bool = False,
) -> str:
    """Build a configuration from an :class:`AeroRunRequest`.

    Reference quantities fall back to the values measured from the CAD when
    the operator did not specify them. The moment origin defaults to the first
    hinge axis point when one is defined, so hinge moments are reported about
    the hinge rather than about an unrelated origin.
    """
    area = reference.reference_area_m2 or measured_area_m2
    length = reference.reference_length_m or measured_length_m

    if reference.moment_origin is not None:
        origin = tuple(float(v) for v in reference.moment_origin)
    elif request.hinge_axes:
        origin = tuple(float(v) for v in request.hinge_axes[0].point)
    else:
        origin = (0.0, 0.0, 0.0)

    context = ConfigContext(
        mesh_filename=mesh_filename,
        reference_area_m2=area,
        reference_length_m=length,
        moment_origin=origin,  # type: ignore[arg-type]
        wall_markers=wall_markers,
        has_symmetry=has_symmetry,
    )
    return build_aero_config(request.flow, request.solver, context)


def write_config(text: str, path: Path | str) -> Path:
    """Write configuration text to disk with Unix line endings.

    SU2's parser is sensitive to stray carriage returns in some builds, so the
    newline translation Python would otherwise apply on Windows is disabled.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="ascii", newline="\n") as handle:
        handle.write(text)
    return path


def parse_config(text: str) -> dict[str, str]:
    """Parse a configuration back into a dictionary.

    Used by the tests and by the GUI when reopening a previous run.
    """
    settings: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("%", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings[key.strip()] = value.strip()
    return settings


def _banner(title: str) -> str:
    """A comment banner separating configuration sections."""
    return f"% ---- {title} " + "-" * max(0, 60 - len(title))


def _yes_no(value: bool) -> str:
    """SU2's boolean spelling."""
    return "YES" if value else "NO"
