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
    solved_now = []
    for point in todo:
        try:
            run_design_point(folder, point, runner, on_line)
            solved_now.append(point.name)
        except Exception as error:  # noqa: BLE001 - one bad point is reported
            failures.append(f"{point.name}: {error}")
    points = cfd_points(store, study_id)
    solved = [p for p in points if p.solved()]
    reports = point_reports(store, study_id)
    summary = {"study_id": study_id, "solved": len(solved), "total": len(points),
               "failures": failures,
               "points_solved_now": [r for r in reports if r.get("name") in solved_now],
               "point_results": point_table(reports),
               "checks": {
                   "not_converged": [r["name"] for r in reports if r.get("converged") is False],
                   "oscillating": [r["name"] for r in reports if r.get("oscillating")],
               },
               "result": None}
    try:
        summary["result"] = _save(store, study_id, analyse(load_params(store, study_id), solved, Sps30Evaluator.SU2))
    except Sps30StudyError as error:
        summary["analysis_pending"] = str(error)
    return summary


def point_reports(store, study_id: str) -> list[dict]:
    """Everything each solved SU2 point recorded (result.json), in order."""
    from backend.shield_workflow import _point_order

    folder = store.get(study_id).path("cfd") / "points"
    reports = []
    if folder.is_dir():
        for path in sorted(folder.glob("*/result.json"), key=lambda p: _point_order(p.parent.name)):
            try:
                reports.append(json.loads(path.read_text()))
            except (OSError, ValueError):  # pragma: no cover - half-written file
                continue
    return reports


def point_table(reports: list[dict]) -> list[dict]:
    """One compact row per solved point."""
    keys = (
        "name", "speed_ms", "yaw_deg", "droplet_um", "face_velocity_ms", "penetration",
        "exchange_flow_lpm", "sensor_hits", "droplets_entered", "converged",
        "oscillating", "residual_drop_orders", "mesh_resolution",
    )
    return [{key: report.get(key) for key in keys} for report in reports]


EXTRA_PREFIX = "X"


def evaluate_conditions(setup, conditions) -> list[dict]:
    """The lumped model at given conditions, one row each."""
    from backend.sps30_study import Sps30AnalyticModel

    model = Sps30AnalyticModel(setup)
    rows = []
    for condition in conditions:
        speed, yaw, droplet = condition.resolve(setup)
        solved = model.solve(speed, yaw, droplet)
        rows.append({"speed_ms": speed, "yaw_deg": yaw, "droplet_um": droplet,
                     **{k: float(v) for k, v in solved.items()}})
    return rows


def add_cfd_points(store, study_id: str, conditions=(), worst_case: bool = False) -> list[Sps30Point]:
    """Append extra design points (X1, ...) to a prepared CFD study.

    ``worst_case`` takes the worst failing condition of the last analysis.
    """
    from backend.sps30_study import write_points_csv

    params = load_params(store, study_id)
    folder = store.get(study_id).path("cfd")
    points = cfd_points(store, study_id)
    wanted = [condition.resolve(params.setup) for condition in conditions]
    if worst_case:
        result = load_result(store, study_id)
        failure = result.first_failure if result is not None else None
        if not failure:
            raise Sps30StudyError("the study has no failing worst case to verify")
        wanted.append((failure["speed_ms"], failure["yaw_deg"], failure["droplet_um"]))
    if not wanted:
        raise Sps30StudyError("give conditions or worst_case")
    taken = {p.name for p in points}
    number, added = 1, []
    for speed, yaw, droplet in wanted:
        while f"{EXTRA_PREFIX}{number}" in taken:
            number += 1
        name = f"{EXTRA_PREFIX}{number}"
        taken.add(name)
        added.append(Sps30Point(name=name, speed_ms=round(float(speed), 6),
                                yaw_deg=round(float(yaw), 6), droplet_um=round(float(droplet), 6)))
    inputs_only = [Sps30Point(name=p.name, speed_ms=p.speed_ms, yaw_deg=p.yaw_deg,
                              droplet_um=p.droplet_um) for p in points]
    write_points_csv(folder / "design_points.csv", inputs_only + added)
    return added
