"""Radiation-shield studies as stored records, shared by the GUI and MCP.

A study is a ``shield-...`` record holding:

* ``params.json`` -- the :class:`ShieldStudyParams`;
* ``cfd/`` -- the SU2 mesh, radiation facets, design points and one
  directory per solved point (once CFD is prepared);
* ``fluent/`` -- the ANSYS Fluent / Workbench package;
* ``result.json`` -- the latest response surface and Monte Carlo analysis.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from backend.shield_study import (
    ShieldStudyError,
    analyse,
    analytic_solver,
    design_of_experiments,
    read_design_points_csv,
    run_analytic_study,
)
from core.shield_models import (
    DesignPoint,
    Evaluator,
    ShieldCfdSettings,
    ShieldStudyParams,
    ShieldStudyResult,
)

PARAMS_FILE = "params.json"
RESULT_FILE = "result.json"
CFD_FOLDER = "cfd"
FLUENT_FOLDER = "fluent"


def _save(store, record_id: str, result: ShieldStudyResult) -> ShieldStudyResult:
    result = result.model_copy(update={"study_id": record_id})
    store.write_json(record_id, RESULT_FILE, result)
    store.update_metadata(
        record_id,
        {
            "evaluator": result.evaluator.value,
            "worst_abs_delta_t_k": result.monte_carlo.abs_max_k,
            "reliability": result.monte_carlo.reliability,
        },
    )
    return result


def new_study(store, params: ShieldStudyParams, label: str = "") -> str:
    """Create an empty study record; returns its id."""
    record = store.create("shield", {"label": label, "bottom_mode": params.setup.bottom_mode.value})
    store.write_json(record.record_id, PARAMS_FILE, params)
    return record.record_id


def load_params(store, study_id: str) -> ShieldStudyParams:
    """The parameters a study was made with."""
    return ShieldStudyParams.model_validate(store.read_json(study_id, PARAMS_FILE))


def load_result(store, study_id: str) -> ShieldStudyResult | None:
    """The latest analysis of a study, if there is one."""
    path = store.get(study_id).path(RESULT_FILE)
    if not path.is_file():
        return None
    return ShieldStudyResult.model_validate(json.loads(path.read_text()))


def run_analytic(store, params: ShieldStudyParams) -> ShieldStudyResult:
    """Instant study: DoE solved by the analytical model, surface, Monte Carlo."""
    study_id = new_study(store, params, "analytic")
    return _save(store, study_id, run_analytic_study(params))


def analyse_imported(
    store, params: ShieldStudyParams, csv_path: Path | str, study_id: str | None = None
) -> ShieldStudyResult:
    """Fit the surface and Monte Carlo to design points solved elsewhere."""
    points = read_design_points_csv(csv_path, params.setup, source="file")
    solved = [p for p in points if p.delta_t_k is not None]
    if not solved:
        raise ShieldStudyError(f"{Path(csv_path).name} contains no solved design points")
    study_id = study_id or new_study(store, params, "imported")
    result = analyse(params, solved, Evaluator.IMPORTED)
    if len(solved) < len(points):
        result.notes.append(
            f"{len(points) - len(solved)} design point(s) in the file had no result "
            "and were left out."
        )
    return _save(store, study_id, result)


def prepare_cfd(
    store,
    params: ShieldStudyParams,
    cfd: ShieldCfdSettings,
    on_line: Callable[[str], None] | None = None,
    export_fluent: bool = True,
) -> dict:
    """Mesh, cast radiation rays, write the SU2 cases and the Fluent package."""
    from backend.shield_cfd import prepare_study

    study_id = new_study(store, params, "cfd")
    record = store.get(study_id)
    points = design_of_experiments(params)
    folder = prepare_study(params.setup, cfd, points, record.path(CFD_FOLDER), on_line)
    geometry = json.loads((folder / "geometry.json").read_text())
    fluent = None
    if export_fluent:
        fluent = export_package(store, study_id)
    store.update_metadata(
        study_id, {"cell_count": geometry["cell_count"], "design_points": len(points)}
    )
    return {
        "study_id": study_id,
        "cfd_folder": str(folder),
        "fluent_folder": str(fluent) if fluent else None,
        "design_points": len(points),
        "cell_count": geometry["cell_count"],
        "mesh_resolution": geometry.get("resolution"),
        "narrowest_air_gap_mm": (
            None if geometry.get("narrowest_gap_m") is None
            else round(1000.0 * geometry["narrowest_gap_m"], 2)
        ),
        "near_cell_size_mm": (
            None if geometry.get("near_size_m") is None
            else round(1000.0 * geometry["near_size_m"], 2)
        ),
        "mesh_sizing_note": geometry.get("sizing_note", ""),
        "thermometer_point_m": [
            round(c + o, 5)
            for c, o in zip(geometry["shield_centre_m"], params.setup.thermometer_xyz_m)
        ],
        "run_script_windows": str(folder / "run_design_points.bat"),
        "run_script_posix": str(folder / "run_design_points.sh"),
    }


def export_package(store, study_id: str) -> Path:
    """Write (or rewrite) the Fluent/Workbench package of a study."""
    from backend.shield_export import export_fluent_package

    return export_fluent_package(load_params(store, study_id), store.get(study_id).path(FLUENT_FOLDER))


def cfd_points(store, study_id: str) -> list[DesignPoint]:
    """A CFD study's design points, with results so far."""
    from backend.shield_cfd import solved_points

    folder = store.get(study_id).path(CFD_FOLDER)
    if not (folder / "study.json").is_file():
        raise ShieldStudyError(f"study {study_id} has no prepared CFD cases")
    return solved_points(folder)


def solve_cfd(
    store,
    study_id: str,
    runner,
    on_line: Callable[[str], None] | None = None,
    max_points: int | None = None,
) -> dict:
    """Solve the unsolved design points with SU2, then analyse what is solved."""
    from backend.shield_cfd import run_design_point

    folder = store.get(study_id).path(CFD_FOLDER)
    points = cfd_points(store, study_id)
    todo = [p for p in points if p.delta_t_k is None]
    if max_points is not None:
        todo = todo[:max_points]
    previous = load_result(store, study_id)
    failures = []
    solved_now = []
    for point in todo:
        try:
            run_design_point(folder, point, runner, on_line)
            solved_now.append(point.name)
        except Exception as error:  # noqa: BLE001 - one bad point is reported, not fatal
            failures.append(f"{point.name}: {error}")
    points = cfd_points(store, study_id)
    solved = [p for p in points if p.delta_t_k is not None]
    reports = point_reports(store, study_id)
    summary = {
        "study_id": study_id,
        "solved": len(solved),
        "total": len(points),
        "failures": failures,
        "points_solved_now": [r for r in reports if r.get("name") in solved_now],
        "point_results": point_table(reports),
        "checks": point_checks(reports),
        "result": None,
    }
    extra = [p for p in solved if p.name in solved_now and p.name.startswith(EXTRA_PREFIX)]
    if extra and previous is not None:
        summary["surface_check"] = surface_check(previous, extra)
    if not solved:
        summary["analysis_pending"] = "no design point is solved yet"
        return summary
    try:
        result = analyse(load_params(store, study_id), solved, Evaluator.SU2)
    except (ShieldStudyError, ValueError, IndexError) as error:
        summary["analysis_pending"] = str(error)
        return summary
    summary["result"] = _save(store, study_id, result)
    return summary


EXTRA_PREFIX = "X"


def evaluate_conditions(setup, conditions) -> list[dict]:
    """The analytical model at given conditions, one row each."""
    from backend.shield_study import ShieldAnalyticModel
    from core.shield_models import BottomMode

    model = ShieldAnalyticModel(setup)
    rows = []
    for condition in conditions:
        wind, solar, bottom = condition.resolve(setup)
        balance = model.solve(wind, solar, bottom)
        row = {
            "wind_speed_ms": wind, "solar_flux_w_m2": solar, "bottom_flux_w_m2": bottom,
            "delta_t_k": balance.delta_t_k, "shield_temp_k": balance.shield_temp_k,
            "monitor_temp_k": balance.monitor_temp_k,
        }
        if setup.bottom_mode is BottomMode.ROOF_TEMPERATURE:
            row["roof_temperature_k"] = setup.roof_temperature_for(bottom)
            row["inlet_warming_k"] = balance.inlet_warming_k
        rows.append(row)
    return rows


def add_cfd_points(store, study_id: str, conditions=(), worst_case: bool = False) -> list[DesignPoint]:
    """Append extra design points (X1, X2, ...) to a prepared CFD study.

    ``worst_case`` takes the inputs of the study's Monte Carlo worst case,
    so the edge of the range -- where a response surface is least
    accurate -- can be checked by a direct solve.
    """
    from backend.shield_study import write_design_points_csv

    params = load_params(store, study_id)
    folder = store.get(study_id).path(CFD_FOLDER)
    points = cfd_points(store, study_id)
    wanted = [condition.resolve(params.setup) for condition in conditions]
    if worst_case:
        result = load_result(store, study_id)
        if result is None:
            raise ShieldStudyError(
                "the study has no analysed result yet: solve its design points first"
            )
        worst = result.monte_carlo.worst_case
        wanted.append((worst["wind_speed_ms"], worst["solar_flux_w_m2"], worst["bottom_flux_w_m2"]))
    if not wanted:
        raise ShieldStudyError("give conditions or worst_case")
    taken = {p.name for p in points}
    number = 1
    added = []
    for wind, solar, bottom in wanted:
        while f"{EXTRA_PREFIX}{number}" in taken:
            number += 1
        name = f"{EXTRA_PREFIX}{number}"
        taken.add(name)
        added.append(DesignPoint(
            name=name, wind_speed_ms=round(float(wind), 6),
            solar_flux_w_m2=round(float(solar), 6), bottom_flux_w_m2=round(float(bottom), 6),
        ))
    # Results live in points/<name>/result.json; the table keeps only inputs.
    unsolved = [p.model_copy(update={"delta_t_k": None}) for p in points]
    write_design_points_csv(folder / "design_points.csv", params.setup, unsolved + added)
    return added


def surface_check(previous, extra: list[DesignPoint]) -> list[dict]:
    """The previous response surface against CFD at the extra points."""
    import numpy as np

    from backend.shield_study import ResponseSurface

    try:
        surface = ResponseSurface(previous.params, previous.design_points)
    except Exception:  # noqa: BLE001 - nothing to compare with
        return []
    rows = []
    for point in extra:
        predicted = float(surface.predict(np.array([point.inputs()]))[0])
        rows.append({
            "name": point.name, "cfd_delta_t_k": point.delta_t_k,
            "surface_delta_t_k": predicted, "difference_k": point.delta_t_k - predicted,
        })
    return rows


def point_reports(store, study_id: str) -> list[dict]:
    """Everything each solved SU2 point recorded (result.json), in order."""
    folder = store.get(study_id).path(CFD_FOLDER) / "points"
    reports = []
    if folder.is_dir():
        for path in sorted(folder.glob("*/result.json"), key=lambda p: _point_order(p.parent.name)):
            try:
                reports.append(json.loads(path.read_text()))
            except (OSError, ValueError):  # pragma: no cover - half-written file
                continue
    return reports


def _point_order(name: str) -> tuple[int, str]:
    digits = "".join(ch for ch in name if ch.isdigit())
    return (int(digits) if digits else 10**9, name)


def point_table(reports: list[dict]) -> list[dict]:
    """One compact row per solved point: inputs, dT and the health checks."""
    keys = (
        "name", "wind_speed_ms", "solar_flux_w_m2", "bottom_flux_w_m2", "delta_t_k",
        "converged", "radiation_settled", "wall_changes_k", "oscillating",
        "residual_drop_orders", "mesh_resolution",
    )
    return [{key: report.get(key) for key in keys} for report in reports]


def point_checks(reports: list[dict]) -> dict:
    """Which solved points need a second look, and why."""
    return {
        "not_converged": [r["name"] for r in reports if not r.get("converged", False)],
        "radiation_not_settled": [
            r["name"] for r in reports if r.get("radiation_settled") is False
        ],
        "oscillating": [r["name"] for r in reports if r.get("oscillating")],
    }


def analytic_check(params: ShieldStudyParams) -> Callable[[DesignPoint], float]:
    """The analytical model as a point solver, for comparisons."""
    return analytic_solver(params.setup)
