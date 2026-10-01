"""SPS30 housing studies as stored records, shared by the GUI and MCP.

A study is an ``sps30-...`` record with ``params.json``, ``cfd/`` (mesh,
design points, one folder per solved point), ``fluent/`` (the Workbench
package) and ``result.json`` (the latest surfaces and Monte Carlo).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from backend.sps30_study import (
    Sps30StudyError,
    analyse,
    design_points,
    read_points_csv,
    run_analytic_study,
)
from core.sps30_models import (
    Sps30CfdSettings,
    Sps30Evaluator,
    Sps30Point,
    Sps30Result,
    Sps30StudyParams,
)

PARAMS_FILE = "params.json"
RESULT_FILE = "result.json"


def _save(store, study_id: str, result: Sps30Result) -> Sps30Result:
    result = result.model_copy(update={"study_id": study_id})
    store.write_json(study_id, RESULT_FILE, result)
    store.update_metadata(
        study_id,
        {"evaluator": result.evaluator.value, "reliability": result.reliability},
    )
    return result


def new_study(store, params: Sps30StudyParams, label: str) -> str:
    record = store.create("sps30", {"label": label})
    store.write_json(record.record_id, PARAMS_FILE, params)
    return record.record_id


def load_params(store, study_id: str) -> Sps30StudyParams:
    return Sps30StudyParams.model_validate(store.read_json(study_id, PARAMS_FILE))


def load_result(store, study_id: str) -> Sps30Result | None:
    path = store.get(study_id).path(RESULT_FILE)
    return Sps30Result.model_validate(json.loads(path.read_text())) if path.is_file() else None


def run_analytic(store, params: Sps30StudyParams) -> Sps30Result:
    return _save(store, new_study(store, params, "analytic"), run_analytic_study(params))


def analyse_imported(store, params: Sps30StudyParams, csv_path, study_id: str | None = None) -> Sps30Result:
    points = read_points_csv(csv_path, params.setup, source="file")
    solved = [p for p in points if p.solved()]
    if not solved:
        raise Sps30StudyError(f"{Path(csv_path).name} contains no fully solved design points")
    study_id = study_id or new_study(store, params, "imported")
    result = analyse(params, solved, Sps30Evaluator.IMPORTED)
    if len(solved) < len(points):
        result.notes.append(f"{len(points) - len(solved)} unsolved row(s) were left out.")
    return _save(store, study_id, result)


def export_package(store, study_id: str) -> Path:
    from backend.sps30_export import export_fluent_package

    return export_fluent_package(load_params(store, study_id), store.get(study_id).path("fluent"))


def prepare_cfd(store, params: Sps30StudyParams, cfd: Sps30CfdSettings,
                on_line: Callable[[str], None] | None = None, export_fluent: bool = True) -> dict:
    from backend.sps30_cfd import Sps30Domain, prepare_study

    study_id = new_study(store, params, "cfd")
    folder = prepare_study(params.setup, cfd, design_points(params), store.get(study_id).path("cfd"), on_line)
    fluent = export_package(store, study_id) if export_fluent else None
    domain = Sps30Domain.load(folder)
    return {
        "study_id": study_id,
        "cfd_folder": str(folder),
        "fluent_folder": str(fluent) if fluent else None,
        "design_points": len(design_points(params)),
        "cell_count": domain.cell_count,
        "run_script_windows": str(folder / "run_design_points.bat"),
    }


def cfd_points(store, study_id: str) -> list[Sps30Point]:
    from backend.sps30_cfd import solved_points

    folder = store.get(study_id).path("cfd")
    if not (folder / "study.json").is_file():
        raise Sps30StudyError(f"study {study_id} has no prepared CFD cases")
    return solved_points(folder)


def solve_cfd(store, study_id: str, runner, on_line=None, max_points: int | None = None) -> dict:
    from backend.sps30_cfd import run_design_point

    folder = store.get(study_id).path("cfd")
    todo = [p for p in cfd_points(store, study_id) if not p.solved()]
    if max_points is not None:
        todo = todo[:max_points]
    failures = []
    for point in todo:
        try:
            run_design_point(folder, point, runner, on_line)
        except Exception as error:  # noqa: BLE001 - one bad point is reported
            failures.append(f"{point.name}: {error}")
    points = cfd_points(store, study_id)
    solved = [p for p in points if p.solved()]
    summary = {"study_id": study_id, "solved": len(solved), "total": len(points),
               "failures": failures, "result": None}
    try:
        summary["result"] = _save(store, study_id, analyse(load_params(store, study_id), solved, Sps30Evaluator.SU2))
    except Sps30StudyError as error:
        summary["analysis_pending"] = str(error)
    return summary
