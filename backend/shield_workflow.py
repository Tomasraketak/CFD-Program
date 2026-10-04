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
    failures = []
    for point in todo:
        try:
            run_design_point(folder, point, runner, on_line)
        except Exception as error:  # noqa: BLE001 - one bad point is reported, not fatal
            failures.append(f"{point.name}: {error}")
    points = cfd_points(store, study_id)
    solved = [p for p in points if p.delta_t_k is not None]
    summary = {
        "study_id": study_id,
        "solved": len(solved),
        "total": len(points),
        "failures": failures,
        "result": None,
    }
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


def analytic_check(params: ShieldStudyParams) -> Callable[[DesignPoint], float]:
    """The analytical model as a point solver, for comparisons."""
    return analytic_solver(params.setup)
