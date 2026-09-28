"""Background workers keeping long operations off the Qt event loop.

Meshing takes tens of seconds and a solve takes minutes. Running either on the
GUI thread freezes the window, so both run on a ``QThread`` and report back
through signals. The solver worker also forwards each parsed iteration, which
is what drives the live residual chart.
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from PySide6 import QtCore

from backend.runner import SolverRunner
from core.models import (
    AeroRunRequest,
    GeometryParams,
    MeshRequest,
    SolverParams,
    ThermalParams,
)


class WorkerSignals(QtCore.QObject):
    """Signals shared by the workers.

    Attributes
    ----------
    progress:
        Human-readable status messages.
    iteration:
        One parsed solver iteration, for live plotting.
    finished:
        The result object on success.
    failed:
        An error message on failure.
    """

    progress = QtCore.Signal(str)
    iteration = QtCore.Signal(object)
    finished = QtCore.Signal(object)
    failed = QtCore.Signal(str)


class _BaseWorker(QtCore.QRunnable):
    """Common error handling for the workers."""

    def __init__(self) -> None:
        super().__init__()
        self.signals = WorkerSignals()
        self._cancelled = False

    def cancel(self) -> None:
        """Request cancellation; effective at the next checkpoint."""
        self._cancelled = True

    def _fail(self, error: BaseException) -> None:
        """Report a failure with enough detail to diagnose it."""
        message = f"{type(error).__name__}: {error}"
        detail = traceback.format_exc(limit=3)
        self.signals.failed.emit(f"{message}\n\n{detail}")


class GeometryPreviewWorker(_BaseWorker):
    """Tessellates a CAD file so the interface can show it.

    Coarse and quick, but still seconds of OpenCASCADE and Gmsh work, which
    is long enough to freeze the window if it ran where the import handler
    runs. ``token`` is handed back untouched so a preview that arrives after
    the operator has opened a different file can be recognised and dropped.
    """

    def __init__(self, geometry: GeometryParams, token: int = 0) -> None:
        super().__init__()
        self.geometry = geometry
        self.token = token

    @QtCore.Slot()
    def run(self) -> None:
        """Tessellate, and report the result with its token."""
        try:
            from backend.mesh_pipeline import tessellate_geometry

            preview = tessellate_geometry(
                self.geometry, notify=self.signals.progress.emit
            )
            self.signals.finished.emit((self.token, preview))
        except BaseException as error:  # noqa: BLE001 - reported to the UI
            self._fail(error)


class MeshWorker(_BaseWorker):
    """Generates a mesh on a background thread."""

    def __init__(self, request: MeshRequest, output_path: Path) -> None:
        super().__init__()
        self.request = request
        self.output_path = Path(output_path)

    @QtCore.Slot()
    def run(self) -> None:
        """Run the meshing pipeline, reporting progress as it goes."""
        try:
            from backend.mesh_pipeline import generate_mesh

            self.signals.progress.emit("Preparing geometry ...")
            result = generate_mesh(
                self.request,
                self.output_path,
                progress=self.signals.progress.emit,
            )
            self.signals.finished.emit(result)
        except BaseException as error:  # noqa: BLE001 - reported to the UI
            self._fail(error)


class AeroWorker(_BaseWorker):
    """Runs an aerodynamic simulation on a background thread."""

    def __init__(
        self,
        request: AeroRunRequest,
        mesh_path: Path,
        working_directory: Path,
        runner: SolverRunner,
        reference_area_m2: float,
        reference_length_m: float,
    ) -> None:
        super().__init__()
        self.request = request
        self.mesh_path = Path(mesh_path)
        self.working_directory = Path(working_directory)
        self.runner = runner
        self.reference_area_m2 = reference_area_m2
        self.reference_length_m = reference_length_m

    def cancel(self) -> None:
        """Stop the solver as well as the worker."""
        super().cancel()
        self.runner.cancel()

    @QtCore.Slot()
    def run(self) -> None:
        """Execute the solve, forwarding iterations for the live chart."""
        try:
            from backend.aero_solver import run_aero_case

            self.signals.progress.emit("Starting solver ...")
            result = run_aero_case(
                self.request,
                mesh_path=self.mesh_path,
                working_directory=self.working_directory,
                runner=self.runner,
                reference_area_m2=self.reference_area_m2,
                reference_length_m=self.reference_length_m,
                sim_id=self.working_directory.name,
                on_iteration=self.signals.iteration.emit,
            )
            self.signals.finished.emit(result)
        except BaseException as error:  # noqa: BLE001 - reported to the UI
            self._fail(error)


class ThermalWorker(_BaseWorker):
    """Runs the sensor microclimate analysis on a background thread.

    In analytic mode this finishes in milliseconds, which is why the tab can
    update its readout live as the operator drags a slider.
    """

    def __init__(
        self,
        params: ThermalParams,
        mesh_path: Path | None = None,
        working_directory: Path | None = None,
        runner: SolverRunner | None = None,
        solver: SolverParams | None = None,
    ) -> None:
        super().__init__()
        self.params = params
        self.mesh_path = Path(mesh_path) if mesh_path else None
        self.working_directory = (
            Path(working_directory) if working_directory else None
        )
        self.runner = runner
        self.solver = solver

    @QtCore.Slot()
    def run(self) -> None:
        """Run the analytical model, or the conjugate solve when meshed."""
        try:
            from backend.thermal_solver import estimate_sensor_bias, run_thermal_case

            if self.mesh_path is None or self.runner is None:
                self.signals.progress.emit("Solving energy balances ...")
                self.signals.finished.emit(estimate_sensor_bias(self.params))
                return

            self.signals.progress.emit("Running conjugate heat transfer ...")
            result = run_thermal_case(
                self.params,
                mesh_path=self.mesh_path,
                working_directory=self.working_directory or Path.cwd(),
                runner=self.runner,
                solver=self.solver,
            )
            self.signals.finished.emit(result)
        except BaseException as error:  # noqa: BLE001 - reported to the UI
            self._fail(error)


class SweepWorker(_BaseWorker):
    """Runs a parametric sweep on a background thread."""

    def __init__(
        self,
        base_request: AeroRunRequest,
        parameter: str,
        values: list[float],
        mesh_path: Path,
        output_root: Path,
        runner: SolverRunner,
        reference_area_m2: float,
        reference_length_m: float,
    ) -> None:
        super().__init__()
        self.base_request = base_request
        self.parameter = parameter
        self.values = values
        self.mesh_path = Path(mesh_path)
        self.output_root = Path(output_root)
        self.runner = runner
        self.reference_area_m2 = reference_area_m2
        self.reference_length_m = reference_length_m

    def cancel(self) -> None:
        """Stop the running case and the sweep."""
        super().cancel()
        self.runner.cancel()

    @QtCore.Slot()
    def run(self) -> None:
        """Run every point, reporting each as it completes."""
        try:
            from backend.sweep import run_parametric_sweep

            total = len(self.values)
            completed = 0

            def on_point(point) -> None:
                nonlocal completed
                completed += 1
                state = "ok" if point.succeeded else "failed"
                self.signals.progress.emit(
                    f"point {completed}/{total}: {self.parameter}="
                    f"{point.value:g} ({state})"
                )
                self.signals.iteration.emit(point)

            result = run_parametric_sweep(
                self.base_request,
                parameter=self.parameter,
                values=self.values,
                mesh_path=self.mesh_path,
                output_root=self.output_root,
                runner=self.runner,
                reference_area_m2=self.reference_area_m2,
                reference_length_m=self.reference_length_m,
                on_point=on_point,
            )
            self.signals.finished.emit(result)
        except BaseException as error:  # noqa: BLE001 - reported to the UI
            self._fail(error)


class TaskWorker(_BaseWorker):
    """Runs any function off the GUI thread.

    The function receives one argument, a callable that reports a line of
    progress; whatever it returns is emitted through ``finished``.
    """

    def __init__(self, function) -> None:
        super().__init__()
        self.function = function

    @QtCore.Slot()
    def run(self) -> None:
        try:
            self.signals.finished.emit(self.function(self.signals.progress.emit))
        except BaseException as error:  # noqa: BLE001 - reported to the UI
            self._fail(error)
