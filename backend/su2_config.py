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

import math
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

# Mach number at and above which a run is treated as properly supersonic and
# gets the cautious start-up numerics below.
MACH_SUPERSONIC_MIN = 1.2

# Mach number from which a subsonic run is started as cautiously as a
# transonic one. Well below the scheme switch at 0.8: on a finned body the
# flow over the nose shoulder and the fin leading edges reaches sonic speed
# locally around freestream Mach 0.7, and a Mach 0.7 case that diverged on
# every mesh it was given was the evidence.
MACH_HIGH_SUBSONIC = 0.6

# Starting CFL by regime. A supersonic cold start is the fragile case: the
# first iterations reconstruct across a shock that does not exist yet, on a
# mesh sized for the converged one. Subsonic JST is far more forgiving and
# pays nothing for a brisker start.
CFL_SUBSONIC = 5.0
CFL_TRANSONIC = 2.0
CFL_SUPERSONIC = 1.0

# (down factor, up factor, min CFL, max CFL) for CFL_ADAPT_PARAM.
#
# The up factor is the load-bearing number. At 2.0 the CFL doubles on every
# successful iteration, so a run starting at 5 is past 150 by iteration six --
# far beyond what a 20-iteration FGMRES/ILU(0) solve can actually solve, at
# which point the "implicit" update is a badly under-relaxed explicit one and
# a prism cell goes negative. 1.05 ramps over a few hundred iterations, which
# is what ramping is supposed to mean. The 0.5 down factor backs off without
# collapsing to the floor and re-ramping in a limit cycle.
#
# Subsonic runs used to keep the doubling ramp on the grounds that JST is
# forgiving. It is not that forgiving: the same ramp drove a Mach 0.7 case to
# a NaN on three different meshes. 1.15 still reaches the cap in about twenty
# iterations, which costs a low-subsonic run next to nothing.
CFL_ADAPT_SUBSONIC = (0.5, 1.15, 0.1, 100.0)
CFL_ADAPT_TRANSONIC = (0.5, 1.10, 0.1, 50.0)
CFL_ADAPT_SUPERSONIC = (0.5, 1.05, 0.1, 25.0)


# Below this Mach number the flow is solved incompressible. A compressible
# solver's numerical dissipation scales with the speed of sound, not the flow
# speed, and at low Mach it swamps the physics: on the Sapphire at Mach 0.1
# JST gave C_d 2.2, and first-order incompressible 2.6, both several times
# the second-order answer. Density changes by under 5 % below Mach 0.3, so
# nothing real is lost.
INCOMPRESSIBLE_MACH_MAX = 0.3

# Incompressible start: gentle, because the second-order pressure-based
# scheme diverged at CFL 10 from a cold start on the same rocket and ran
# cleanly at 2 on a slow ramp.
CFL_INCOMPRESSIBLE = 2.0
CFL_ADAPT_INCOMPRESSIBLE = (0.5, 1.05, 0.5, 50.0)


def solves_incompressible(mach: float, solver: SolverParams) -> bool:
    """Whether a run is solved with the incompressible solver.

    Only when the operator left the scheme to the program: asking for a
    compressible scheme by name is honoured.
    """
    return mach < INCOMPRESSIBLE_MACH_MAX and solver.convective_scheme is None


def default_cfl(mach: float) -> float:
    """The starting CFL for a Mach number, when the caller did not choose one.

    An explicitly requested CFL is always honoured verbatim; this only fills
    the blank.
    """
    if mach < MACH_HIGH_SUBSONIC:
        return CFL_SUBSONIC
    if mach < MACH_SUPERSONIC_MIN:
        return CFL_TRANSONIC
    return CFL_SUPERSONIC


def default_cfl_adapt_param(mach: float) -> tuple[float, float, float, float]:
    """The CFL adaption quadruple for a Mach number."""
    if mach < MACH_HIGH_SUBSONIC:
        return CFL_ADAPT_SUBSONIC
    if mach < MACH_SUPERSONIC_MIN:
        return CFL_ADAPT_TRANSONIC
    return CFL_ADAPT_SUPERSONIC


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
    history_filename: str = "history"
    output_write_frequency: int = 250


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
    incompressible = solves_incompressible(mach, solver)
    scheme = select_convective_scheme(mach, solver.convective_scheme)
    upwind = uses_upwind(scheme) or incompressible

    lines: list[str] = []
    add = lines.append

    add(_banner("AeroThermalStudio - aerodynamic run"))
    add(f"% Mach {mach:.4f}, alpha {flow.aoa_deg:g} deg, beta {flow.sideslip_deg:g} deg")
    add(f"% Freestream {state.pressure_pa:.1f} Pa, {state.temperature_k:.2f} K")
    add("")

    add(_banner("Problem definition"))
    add("SOLVER= INC_RANS" if incompressible else "SOLVER= RANS")
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
    if incompressible:
        # MACH_NUMBER, AOA and SIDESLIP_ANGLE above stay: the incompressible
        # solver ignores the Mach number but projects lift and drag with the
        # angles, and the images read their caption from them.
        speed = flow.speed_ms()
        alpha = math.radians(flow.aoa_deg)
        beta = math.radians(flow.sideslip_deg)
        velocity = (
            speed * math.cos(alpha) * math.cos(beta),
            speed * math.sin(beta),
            speed * math.sin(alpha) * math.cos(beta),
        )
        add("INC_NONDIM= DIMENSIONAL")
        add("INC_DENSITY_MODEL= CONSTANT")
        add(f"INC_DENSITY_INIT= {state.density_kg_m3:.6f}")
        add("INC_VELOCITY_INIT= ( {:.6f}, {:.6f}, {:.6f} )".format(*velocity))
        add("INC_ENERGY_EQUATION= NO")
        add(f"INC_TEMPERATURE_INIT= {state.temperature_k:.6f}")
        add("VISCOSITY_MODEL= CONSTANT_VISCOSITY")
        add(f"MU_CONSTANT= {state.viscosity_pa_s:.9e}")
    else:
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
    # Weighted least squares is ill-conditioned across the jump from
    # high-aspect-ratio prisms to isotropic tets that a hybrid mesh has at the
    # top of its boundary layer. One sliver cell with a bad stencil is among
    # the commonest causes of an early NaN. JST reconstructs nothing, but the
    # viscous fluxes and the SST source terms use the same gradients, so the
    # central branch gets the robust choice too.
    add("NUM_METHOD_GRAD= GREEN_GAUSS")
    if upwind:
        add("NUM_METHOD_GRAD_RECON= GREEN_GAUSS")
    # FDS is the incompressible solver's upwind flux.
    add(f"CONV_NUM_METHOD_FLOW= {'FDS' if incompressible else scheme.value}")
    if upwind:
        add(f"MUSCL_FLOW= {_yes_no(solver.muscl)}")
        add(f"SLOPE_LIMITER_FLOW= {solver.limiter}")
        add(f"VENKAT_LIMITER_COEFF= {VENKAT_LIMITER_COEFF}")
        if scheme is ConvectiveScheme.ROE and not incompressible:
            # Entropy fix, needed to keep the Roe solver from admitting
            # expansion shocks at supersonic Mach numbers. 0.05 is the usual
            # production value once a solution exists; it is thin during a
            # start-up where u-c changes sign erratically at the nose shock
            # and the fin leading edges, so a properly supersonic run gets
            # more. Above roughly 0.2 the shock itself starts to smear.
            fix = 0.10 if mach >= MACH_SUPERSONIC_MIN else 0.05
            add(f"ENTROPY_FIX_COEFF= {fix:g}")
    else:
        # JST's 2nd and 4th order dissipation coefficients.
        add("JST_SENSOR_COEFF= ( 0.5, 0.02 )")
    add("TIME_DISCRE_FLOW= EULER_IMPLICIT")

    add("CONV_NUM_METHOD_TURB= SCALAR_UPWIND")
    add("MUSCL_TURB= NO")
    add("TIME_DISCRE_TURB= EULER_IMPLICIT")
    if upwind or mach >= MACH_HIGH_SUBSONIC:
        # The SST omega source is stiff at a cold start, where omega spans
        # orders of magnitude in the first cells off the wall. Decoupling the
        # turbulence CFL from the flow CFL is the cheapest stabilisation
        # available.
        add("CFL_REDUCTION_TURB= 0.5")
    # Written explicitly rather than left to defaults: SU2's 5% freestream
    # turbulence is not physical for atmospheric flight, and pinning it keeps
    # results comparable across SU2 builds.
    add("FREESTREAM_TURBULENCEINTENSITY= 0.01")
    add("FREESTREAM_TURB2LAMVISCRATIO= 10.0")
    add("")

    add(_banner("Wall model"))
    # These are the v8 defaults, written out so a future SU2 changing one
    # cannot silently change our results.
    add("WALLMODEL_KAPPA= 0.41")
    add("WALLMODEL_B= 5.5")
    add("WALLMODEL_MAXITER= 200")
    add("WALLMODEL_RELFAC= 0.5")
    add("")

    add(_banner("Linear solver"))
    add("LINEAR_SOLVER= FGMRES")
    add("LINEAR_SOLVER_PREC= ILU")
    add("LINEAR_SOLVER_ILU_FILL_IN= 0")
    # 1E-6 is unreachable in ten FGMRES iterations on an ILU(0)-preconditioned
    # RANS Jacobian, so the old tolerance was decorative and the solve always
    # burned its iteration cap anyway. 20 iterations at 1E-4 is the usual
    # production pairing and gives a genuinely implicit update.
    add("LINEAR_SOLVER_ERROR= 1E-4")
    add("LINEAR_SOLVER_ITER= 20")
    add("")

    add(_banner("Convergence"))
    regime_cfl = CFL_INCOMPRESSIBLE if incompressible else default_cfl(mach)
    cfl = solver.cfl_number if solver.cfl_number is not None else regime_cfl
    add(f"CFL_NUMBER= {cfl:g}")
    add(f"CFL_ADAPT= {_yes_no(solver.cfl_adapt)}")
    if solver.cfl_adapt:
        down, up, cfl_min, cfl_max = (
            CFL_ADAPT_INCOMPRESSIBLE if incompressible else default_cfl_adapt_param(mach)
        )
        # An explicit ramp rate or ceiling wins. Without these, a starting
        # CFL of 0.5 was only a starting point: at a doubling ramp it was
        # back at 100 within eight iterations, which is how a "low-CFL"
        # rescue stage was nothing of the kind.
        if solver.cfl_growth is not None:
            up = solver.cfl_growth
        if solver.cfl_max is not None:
            cfl_max = solver.cfl_max
        cfl_max = max(cfl_max, cfl)
        # The floor is a constant rather than a fraction of the start, so
        # lowering the starting CFL cannot silently drop it into uselessness.
        add(
            f"CFL_ADAPT_PARAM= ( {down:g}, {up:g}, "
            f"{min(cfl_min, cfl):g}, {cfl_max:g} )"
        )
    add(f"ITER= {solver.max_iterations}")
    # Relative to the peak, not absolute: see relative_residual_drop. And
    # never before a hundred iterations, when the forces have not formed.
    # The incompressible solver's continuity residual is the pressure one.
    add(f"CONV_FIELD= {'REL_RMS_PRESSURE' if incompressible else 'REL_RMS_DENSITY'}")
    add(f"CONV_RESIDUAL_MINVAL= {solver.convergence_residual:g}")
    add("CONV_STARTITER= 100")
    add("")

    add(_banner("Input / output"))
    add(f"MESH_FILENAME= {context.mesh_filename}")
    add("MESH_FORMAT= SU2")
    add("TABULAR_FORMAT= CSV")
    add(f"CONV_FILENAME= {context.history_filename}")
    add("VOLUME_FILENAME= flow")
    add("SURFACE_FILENAME= surface_flow")
    add("RESTART_FILENAME= restart_flow.dat")
    if context.restart_filename:
        add(f"SOLUTION_FILENAME= {context.restart_filename}")
    add("OUTPUT_FILES= ( RESTART, PARAVIEW_MULTIBLOCK, SURFACE_CSV )")
    add("WRT_VOLUME_OVERWRITE= YES")
    # Without this the restart lands as restart_flow_00400.dat and anything
    # that expects to find restart_flow.dat -- the rescue's stage handover,
    # among others -- fails on a missing file.
    add("WRT_RESTART_OVERWRITE= YES")
    add("READ_BINARY_RESTART= YES")
    add(f"OUTPUT_WRT_FREQ= {context.output_write_frequency}")
    residuals = "RMS_PRESSURE, RMS_VELOCITY-X" if incompressible else "RMS_DENSITY, RMS_ENERGY"
    add(
        f"SCREEN_OUTPUT= ( INNER_ITER, {residuals}, LIFT, DRAG, "
        "SIDEFORCE, MOMENT_X, MOMENT_Y, MOMENT_Z )"
    )
    add(
        "HISTORY_OUTPUT= ( ITER, RMS_RES, REL_RMS_RES, AERO_COEFF, FORCES_BREAKDOWN )"
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
    solver: SolverParams | None = None,
    restart_filename: str | None = None,
    history_filename: str = "history",
    output_write_frequency: int = 250,
) -> str:
    """Build a configuration from an :class:`AeroRunRequest`.

    Reference quantities fall back to the values measured from the CAD when
    the operator did not specify them. The moment origin defaults to the first
    hinge axis point when one is defined, so hinge moments are reported about
    the hinge rather than about an unrelated origin.

    ``solver`` overrides the request's own settings, which is how the rescue
    runs a first-order stage without altering what the operator asked for.
    """
    area = reference.reference_area_m2 or measured_area_m2
    length = reference.reference_length_m or measured_length_m

    if reference.moment_origin is not None:
        origin = tuple(float(v) for v in reference.moment_origin)
    elif request.hinge_axes:
        origin = tuple(float(v) for v in request.hinge_axes[0].solver_point())
    else:
        origin = (0.0, 0.0, 0.0)

    context = ConfigContext(
        mesh_filename=mesh_filename,
        reference_area_m2=area,
        reference_length_m=length,
        moment_origin=origin,  # type: ignore[arg-type]
        wall_markers=wall_markers,
        has_symmetry=has_symmetry,
        restart_filename=restart_filename,
        history_filename=history_filename,
        output_write_frequency=output_write_frequency,
    )
    return build_aero_config(request.flow, solver or request.solver, context)


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
