"""SPS30 housing study in the GUI: every setting, the runs and the results.

The same study runs from the assistant through the ``sps30_housing_study``
tool; studies are ``sps30-...`` records either side can open.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets

from core.sps30_models import (
    SPS30_LABELS,
    SPS30_OUTPUT_LABELS,
    SPS30_OUTPUTS,
    SPS30_VARIABLES,
    Sps30CfdSettings,
    Sps30Result,
    Sps30Setup,
    Sps30StudyParams,
    Sps30Variable,
)
from gui.shield_panel import (
    _combo,
    _int_spin,
    _optional,
    _optional_spin,
    _set_optional,
    _spin,
    _wrap,
)
from gui.theme import CHART_COLOURS, DANGER, SUCCESS, WARNING

try:  # pragma: no cover - import guard
    import pyqtgraph as pg

    HAVE_CHARTS = True
except Exception:  # pragma: no cover
    pg = None  # type: ignore[assignment]
    HAVE_CHARTS = False

AXES = ["+x", "-x", "+y", "-y", "+z", "-z"]


class Sps30StudyPanel(QtWidgets.QWidget):
    """Controls, results and charts for SPS30 housing studies."""

    statusMessage = QtCore.Signal(str)

    def __init__(self, store, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.pool = QtCore.QThreadPool.globalInstance()
        self._workers: list[Any] = []
        self._result: Sps30Result | None = None
        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls = self._build_controls()
        controls.setMinimumWidth(470)
        scroll.setMinimumWidth(500)
        scroll.setWidget(controls)
        splitter.addWidget(scroll)
        splitter.addWidget(self._build_results())
        splitter.setSizes([480, 900])
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self.refresh_studies()

    # ------------------------------------------------------------ controls

    def _build_controls(self) -> QtWidgets.QWidget:
        from gui.main_window import VectorInput

        d = Sps30StudyParams()
        s = d.setup
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)

        group = QtWidgets.QGroupBox("Housing and sensor")
        form = QtWidgets.QFormLayout(group)
        self.step_path = QtWidgets.QLineEdit()
        self.step_path.editingFinished.connect(lambda: self._step_changed())
        self.step_path.setPlaceholderText("Empty: built-in 120 x 70 x 80 mm housing")
        browse = QtWidgets.QPushButton("Browse")
        browse.clicked.connect(self._browse)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.step_path, 1)
        row.addWidget(browse)
        form.addRow("Housing STEP", _wrap(row))
        self.scale = _optional_spin(1.0e3, decimals=6)
        form.addRow("Scale to metres", self.scale)
        self.forward = _combo(["+x", "-x", "+y", "-y"], s.forward_axis)
        self.forward.setToolTip("Direction the platform travels in the CAD; the wind comes from it")
        form.addRow("Travel direction", self.forward)
        self.sensor_center = VectorInput(tuple(s.sensor_face_center_m))
        form.addRow("Sensor face centre", self.sensor_center)
        self.sensor_normal = _combo(AXES, s.sensor_face_normal)
        form.addRow("Sensor face looks", self.sensor_normal)
        self.sensor_a = _spin(0.001, 1.0, s.sensor_face_size_m[0], 4, 0.001, " m")
        self.sensor_b = _spin(0.001, 1.0, s.sensor_face_size_m[1], 4, 0.001, " m")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.sensor_a)
        row.addWidget(self.sensor_b)
        form.addRow("Sensor face size", _wrap(row))
        self.chamber_x = _spin(-10.0, 10.0, s.chamber_plane_x_m, 4, 0.005, " m")
        self.chamber_x.setToolTip("x (CAD) of the plane the chamber exchange flow is measured on")
        form.addRow("Chamber plane x", self.chamber_x)
        self.fan = QtWidgets.QCheckBox("Model the SPS30 fan")
        self.fan.setChecked(s.fan_enabled)
        form.addRow("", self.fan)
        self.fan_flow = _spin(0.01, 20.0, s.fan_flow_lpm, 3, 0.05, " L/min")
        form.addRow("Fan flow", self.fan_flow)
        layout.addWidget(group)

        group = QtWidgets.QGroupBox("Tunnel, air and goals")
        form = QtWidgets.QFormLayout(group)
        mult = s.domain_multipliers
        self.upstream = _spin(1.0, 50.0, mult["upstream"], 1, 0.5, " L")
        self.downstream = _spin(1.0, 50.0, mult["downstream"], 1, 0.5, " L")
        self.lateral = _spin(1.0, 50.0, mult["lateral"], 1, 0.5, " L")
        self.vertical = _spin(1.0, 50.0, mult["vertical"], 1, 0.5, " L")
        for label, box in (("Upstream", self.upstream), ("Downstream", self.downstream),
                           ("Lateral", self.lateral), ("Vertical", self.vertical)):
            form.addRow(label, box)
        self.ambient = _spin(-40.0, 50.0, s.ambient_temp_c, 1, 1.0, " C")
        form.addRow("Air temperature", self.ambient)
        self.speed = _spin(0.1, 80.0, s.speed_ms, 2, 1.0, " m/s")
        form.addRow("Baseline speed", self.speed)
        self.yaw = _spin(-45.0, 45.0, s.yaw_deg, 1, 1.0, " deg")
        form.addRow("Baseline yaw", self.yaw)
        self.droplet = _spin(0.1, 5000.0, s.droplet_um, 1, 10.0, " um")
        form.addRow("Baseline droplet", self.droplet)
        self.max_face = _spin(0.01, 20.0, s.max_face_velocity_ms, 2, 0.1, " m/s")
        form.addRow("Goal: face velocity below", self.max_face)
        self.max_pen = _spin(0.0, 1.0, s.max_penetration, 4, 0.001)
        form.addRow("Goal: penetration at most", self.max_pen)
        self.min_flow = _spin(0.0, 100.0, s.min_exchange_flow_lpm, 2, 0.1, " L/min")
        self.min_flow.setToolTip("0 = exchange flow is reported but not a pass/fail goal")
        form.addRow("Goal: exchange flow at least", self.min_flow)
        layout.addWidget(group)

        group = QtWidgets.QGroupBox("Lumped model dimensions")
        form = QtWidgets.QFormLayout(group)
        self.port_area = _spin(1e-7, 1.0, s.port_area_m2, 7, 1e-6, " m2")
        form.addRow("One slit area", self.port_area)
        self.port_width = _spin(1e-4, 1.0, s.port_width_m, 4, 0.0005, " m")
        form.addRow("Slit width", self.port_width)
        self.plenum_area = _spin(1e-6, 1.0, s.plenum_area_m2, 6, 1e-4, " m2")
        form.addRow("Plenum cross-section", self.plenum_area)
        self.baffle_gap = _spin(1e-4, 1.0, s.baffle_gap_m, 4, 0.001, " m")
        form.addRow("Baffle gap", self.baffle_gap)
        self.chamber_area = _spin(1e-6, 1.0, s.chamber_area_m2, 6, 1e-4, " m2")
        form.addRow("Chamber cross-section", self.chamber_area)
        self.chamber_fraction = _spin(0.01, 1.0, s.chamber_fraction, 2, 0.05)
        form.addRow("Chamber share of flow", self.chamber_fraction)
        layout.addWidget(group)

        group = QtWidgets.QGroupBox("Uncertain inputs")
        box = QtWidgets.QVBoxLayout(group)
        columns = ("Min", "Max", "Distribution", "Mean", "Std", "Mode", "Log")
        self.variables = QtWidgets.QTableWidget(3, len(columns))
        self.variables.setHorizontalHeaderLabels(list(columns))
        self.variables.setVerticalHeaderLabels([SPS30_LABELS[n].split(" [")[0] for n in SPS30_VARIABLES])
        self.variables.setMinimumHeight(130)
        self._vars: dict[str, dict[str, QtWidgets.QWidget]] = {}
        for r, name in enumerate(SPS30_VARIABLES):
            v = getattr(d, name)
            log = QtWidgets.QCheckBox()
            log.setChecked(v.log)
            widgets = {
                "minimum": _spin(-1e6, 1e6, v.minimum, 3, 1.0),
                "maximum": _spin(-1e6, 1e6, v.maximum, 3, 1.0),
                "distribution": _combo(["uniform", "normal", "triangular"], v.distribution),
                "mean": _optional_spin(1e6),
                "std": _optional_spin(1e6),
                "mode": _optional_spin(1e6),
                "log": log,
            }
            for c, key in enumerate(widgets):
                self.variables.setCellWidget(r, c, widgets[key])
            self._vars[name] = widgets
        self.variables.resizeColumnsToContents()
        box.addWidget(self.variables)
        layout.addWidget(group)

        group = QtWidgets.QGroupBox("Design exploration")
        form = QtWidgets.QFormLayout(group)
        self.doe = QtWidgets.QComboBox()
        self.doe.addItem("Central composite, face-centred (15 points)", "ccd")
        self.doe.addItem("Latin hypercube", "lhs")
        form.addRow("Design of experiments", self.doe)
        self.doe_points = _int_spin(6, 500, d.doe_points)
        form.addRow("LHS points", self.doe_points)
        self.surrogate = QtWidgets.QComboBox()
        self.surrogate.addItem("Radial basis (Kriging-like)", "rbf")
        self.surrogate.addItem("Full quadratic polynomial", "quadratic")
        form.addRow("Response surface", self.surrogate)
        self.samples = _int_spin(100, 2_000_000, d.monte_carlo_samples, 1000)
        form.addRow("Monte Carlo samples", self.samples)
        self.seed = _int_spin(0, 2**31 - 1, d.seed)
        form.addRow("Random seed", self.seed)
        layout.addWidget(group)

        c = Sps30CfdSettings()
        group = QtWidgets.QGroupBox("SU2 CFD + droplet tracking")
        form = QtWidgets.QFormLayout(group)
        self.resolution = _combo(["coarse", "medium", "fine"], c.mesh_resolution)
        form.addRow("Mesh", self.resolution)
        self.iterations = _int_spin(50, 100_000, c.iterations, 100)
        form.addRow("Iterations", self.iterations)
        self.ranks = _int_spin(1, 256, c.mpi_ranks)
        form.addRow("MPI ranks", self.ranks)
        self.droplets = _int_spin(10, 200_000, c.droplets, 500)
        form.addRow("Droplets per point", self.droplets)
        self.random_walk = QtWidgets.QCheckBox("Discrete random walk (turbulent dispersion)")
        self.random_walk.setChecked(c.random_walk)
        form.addRow("", self.random_walk)
        self.cfd_seed = _int_spin(0, 2**31 - 1, c.seed)
        form.addRow("Droplet seed", self.cfd_seed)
        self.solve_limit = _int_spin(0, 500, 0)
        self.solve_limit.setSpecialValueText("all")
        form.addRow("Points to solve now", self.solve_limit)
        layout.addWidget(group)

        from gui.study_extras import SweepControls

        self.sweep_controls = SweepControls(
            {name: SPS30_LABELS[name] for name in SPS30_VARIABLES},
            {
                "speed_ms": (5.0, 35.0, 2.5),
                "yaw_deg": (-20.0, 20.0, 5.0),
                "droplet_um": (10.0, 200.0, 10.0),
            },
        )
        self.sweep_controls.run_button.clicked.connect(self.run_sweep)
        self.sweep_controls.preview_button.clicked.connect(self.draw_geometry)
        layout.addWidget(self.sweep_controls)

        self.run_button = QtWidgets.QPushButton("Run analytic study")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self.run_analytic)
        self.prepare_button = QtWidgets.QPushButton("Prepare CFD cases + Fluent package")
        self.prepare_button.clicked.connect(self.prepare_cfd)
        self.solve_button = QtWidgets.QPushButton("Solve CFD design points (SU2 + droplets)")
        self.solve_button.clicked.connect(self.solve_cfd)
        self.import_button = QtWidgets.QPushButton("Import solved design points (CSV)...")
        self.import_button.clicked.connect(self.import_csv)
        self.open_button = QtWidgets.QPushButton("Open study folder")
        self.open_button.clicked.connect(self.open_folder)
        for b in (self.run_button, self.prepare_button, self.solve_button, self.import_button, self.open_button):
            layout.addWidget(b)
        layout.addStretch(1)
        return panel

    def _build_results(self) -> QtWidgets.QWidget:
        from gui.main_window import ResultCard

        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Study"))
        self.study_list = QtWidgets.QComboBox()
        self.study_list.currentIndexChanged.connect(self._study_selected)
        row.addWidget(self.study_list, 1)
        refresh = QtWidgets.QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_studies)
        row.addWidget(refresh)
        layout.addLayout(row)

        cards = QtWidgets.QHBoxLayout()
        self.card_face = ResultCard("Worst face velocity", "m/s")
        self.card_pen = ResultCard("Worst penetration", "%")
        self.card_flow = ResultCard("Lowest exchange flow", "L/min")
        self.card_reliability = ResultCard("Reliability", "%")
        for card in (self.card_face, self.card_pen, self.card_flow, self.card_reliability):
            cards.addWidget(card)
        layout.addLayout(cards)
        self.summary = QtWidgets.QLabel("Run a study to see the Monte Carlo results.")
        self.summary.setWordWrap(True)
        self.summary.setObjectName("hint")
        self.summary.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Histogram of"))
        self.chart_output = QtWidgets.QComboBox()
        for name in SPS30_OUTPUTS:
            self.chart_output.addItem(SPS30_OUTPUT_LABELS[name], name)
        self.chart_output.currentIndexChanged.connect(lambda *_: self._draw())
        row.addWidget(self.chart_output, 1)
        layout.addLayout(row)
        if HAVE_CHARTS:
            self.histogram = pg.PlotWidget()
            self.histogram.setLabel("left", "samples")
            layout.addWidget(self.histogram, 2)
        else:  # pragma: no cover
            self.histogram = None
        self.points_table = QtWidgets.QTableWidget(0, 7)
        self.points_table.setHorizontalHeaderLabels(
            ["Point", "Speed [m/s]", "Yaw [deg]", "Droplet [um]", "Face v [m/s]", "Penetration", "Exchange [L/min]"]
        )
        self.points_table.horizontalHeader().setStretchLastSection(True)
        from gui.study_extras import results_tabs

        self.result_tabs, self.sweep_view, self.geometry_view = results_tabs(self.points_table)
        layout.addWidget(self.result_tabs, 2)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        self.log.setMaximumHeight(130)
        layout.addWidget(self.log)
        return panel

    # -------------------------------------------------------------- params

    def _browse(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Housing STEP", "", "STEP files (*.step *.stp)")
        if path:
            self.step_path.setText(path)
            self._step_changed()

    def _step_changed(self) -> None:
        """Draw the newly chosen housing so it can be checked at once."""
        try:
            params = self.study_params()
        except Exception:  # noqa: BLE001 - the form is mid-edit
            return
        self._draw_geometry_for(params.setup)

    def study_params(self) -> Sps30StudyParams:
        setup = Sps30Setup(
            housing_step_path=self.step_path.text().strip(),
            scale_to_meters=_optional(self.scale),
            sensor_face_center_m=self.sensor_center.value(),
            sensor_face_normal=self.sensor_normal.currentData(),
            sensor_face_size_m=[self.sensor_a.value(), self.sensor_b.value()],
            chamber_plane_x_m=self.chamber_x.value(),
            fan_enabled=self.fan.isChecked(),
            fan_flow_lpm=self.fan_flow.value(),
            forward_axis=self.forward.currentData(),
            domain_multipliers={
                "upstream": self.upstream.value(), "downstream": self.downstream.value(),
                "lateral": self.lateral.value(), "vertical": self.vertical.value(),
            },
            ambient_temp_c=self.ambient.value(),
            speed_ms=self.speed.value(),
            yaw_deg=self.yaw.value(),
            droplet_um=self.droplet.value(),
            port_area_m2=self.port_area.value(),
            port_width_m=self.port_width.value(),
            plenum_area_m2=self.plenum_area.value(),
            baffle_gap_m=self.baffle_gap.value(),
            chamber_area_m2=self.chamber_area.value(),
            chamber_fraction=self.chamber_fraction.value(),
            max_face_velocity_ms=self.max_face.value(),
            max_penetration=self.max_pen.value(),
            min_exchange_flow_lpm=self.min_flow.value(),
        )
        variables = {
            name: Sps30Variable(
                minimum=w["minimum"].value(), maximum=w["maximum"].value(),
                distribution=w["distribution"].currentData(),
                mean=_optional(w["mean"]), std=_optional(w["std"]), mode=_optional(w["mode"]),
                log=w["log"].isChecked(),
            )
            for name, w in self._vars.items()
        }
        return Sps30StudyParams(
            setup=setup, **variables,
            doe=self.doe.currentData(), doe_points=self.doe_points.value(),
            surrogate=self.surrogate.currentData(), monte_carlo_samples=self.samples.value(),
            seed=self.seed.value(),
        )

    def cfd_settings(self) -> Sps30CfdSettings:
        return Sps30CfdSettings(
            mesh_resolution=self.resolution.currentData(), iterations=self.iterations.value(),
            mpi_ranks=self.ranks.value(), droplets=self.droplets.value(),
            random_walk=self.random_walk.isChecked(), seed=self.cfd_seed.value(),
        )

    def apply_params(self, p: Sps30StudyParams) -> None:
        s = p.setup
        self.step_path.setText(s.housing_step_path)
        _set_optional(self.scale, s.scale_to_meters)
        self.sensor_center.set_value(tuple(s.sensor_face_center_m))
        self.sensor_normal.setCurrentIndex(self.sensor_normal.findData(s.sensor_face_normal))
        self.sensor_a.setValue(s.sensor_face_size_m[0])
        self.sensor_b.setValue(s.sensor_face_size_m[1])
        self.chamber_x.setValue(s.chamber_plane_x_m)
        self.fan.setChecked(s.fan_enabled)
        self.fan_flow.setValue(s.fan_flow_lpm)
        self.forward.setCurrentIndex(self.forward.findData(s.forward_axis))
        for key, box in (("upstream", self.upstream), ("downstream", self.downstream),
                         ("lateral", self.lateral), ("vertical", self.vertical)):
            box.setValue(s.domain_multipliers.get(key, box.value()))
        self.ambient.setValue(s.ambient_temp_c)
        self.speed.setValue(s.speed_ms)
        self.yaw.setValue(s.yaw_deg)
        self.droplet.setValue(s.droplet_um)
        self.port_area.setValue(s.port_area_m2)
        self.port_width.setValue(s.port_width_m)
        self.plenum_area.setValue(s.plenum_area_m2)
        self.baffle_gap.setValue(s.baffle_gap_m)
        self.chamber_area.setValue(s.chamber_area_m2)
        self.chamber_fraction.setValue(s.chamber_fraction)
        self.max_face.setValue(s.max_face_velocity_ms)
        self.max_pen.setValue(s.max_penetration)
        self.min_flow.setValue(s.min_exchange_flow_lpm)
        for name, w in self._vars.items():
            v = getattr(p, name)
            w["minimum"].setValue(v.minimum)
            w["maximum"].setValue(v.maximum)
            w["distribution"].setCurrentIndex(w["distribution"].findData(v.distribution))
            _set_optional(w["mean"], v.mean)
            _set_optional(w["std"], v.std)
            _set_optional(w["mode"], v.mode)
            w["log"].setChecked(v.log)
        self.doe.setCurrentIndex(self.doe.findData(p.doe))
        self.doe_points.setValue(p.doe_points)
        self.surrogate.setCurrentIndex(self.surrogate.findData(p.surrogate))
        self.samples.setValue(p.monte_carlo_samples)
        self.seed.setValue(p.seed)

    # ------------------------------------------------------------- actions

    def _params_or_warn(self) -> Sps30StudyParams | None:
        try:
            return self.study_params()
        except Exception as error:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Invalid settings", str(error))
            return None

    def _start(self, function, done, label: str) -> None:
        from gui.workers import TaskWorker

        worker = TaskWorker(function)
        worker.signals.progress.connect(self.append_log)
        worker.signals.finished.connect(done)
        worker.signals.finished.connect(lambda *_: self._set_busy(False))
        worker.signals.failed.connect(self._failed)
        self._workers.append(worker)
        self._set_busy(True)
        self.append_log(label)
        self.statusMessage.emit(label)
        self.pool.start(worker)

    def _set_busy(self, busy: bool) -> None:
        for b in (self.run_button, self.prepare_button, self.solve_button, self.import_button):
            b.setEnabled(not busy)

    def _failed(self, message: str) -> None:
        self._set_busy(False)
        self.append_log(message)
        QtWidgets.QMessageBox.warning(self, "SPS30 housing study", message.split("\n\n")[0])

    def append_log(self, line: str) -> None:
        self.log.appendPlainText(line.rstrip())

    def run_analytic(self) -> None:
        params = self._params_or_warn()
        if params is None:
            return
        from backend import sps30_workflow

        self._start(lambda _p: sps30_workflow.run_analytic(self.store, params), self._show, "Running the analytic study ...")

    def run_sweep(self) -> None:
        """Step one input with the analytical model and show every point."""
        params = self._params_or_warn()
        if params is None:
            return
        from backend import parameter_sweep as sweeps
        from core.platform_env import data_root

        variable, start, stop, step = self.sweep_controls.values()
        try:
            sweeps.sweep_values(start, stop, step)
        except sweeps.SweepError as error:
            QtWidgets.QMessageBox.warning(self, "Sweep", str(error))
            return
        self._start(
            lambda progress: sweeps.save_sweep(
                sweeps.sps30_sweep(params.setup, variable, start, stop, step),
                data_root() / "sweeps",
            ),
            self._show_sweep,
            f"Sweeping {variable} from {start:g} to {stop:g} in steps of {step:g} ...",
        )

    def _show_sweep(self, result) -> None:
        self.sweep_view.show_result(result)
        self.result_tabs.setCurrentWidget(self.sweep_view)
        for note in result.notes:
            self.append_log(note)
        self.statusMessage.emit(f"Sweep: {len(result.rows)} points solved")

    def draw_geometry(self) -> None:
        """Draw the housing outside and cut open, with the air and the fan."""
        params = self._params_or_warn()
        if params is None:
            return
        self._draw_geometry_for(params.setup, switch=True)

    def _draw_geometry_for(self, setup, switch: bool = False) -> None:
        from backend.study_preview import cached_preview
        from gui.study_extras import run_in_background

        from backend.study_preview import preview_key

        key = preview_key("sps30", setup)
        self._geometry_request = key

        def done(path) -> None:
            if key != self._geometry_request:
                return
            self.geometry_view.show_image(path)
            self.geometry_view.key = key
            if switch:
                self.result_tabs.setCurrentWidget(self.geometry_view)

        run_in_background(
            self, lambda: cached_preview("sps30", setup), done, "Drawing the housing geometry ..."
        )

    def run_analytic_now(self) -> Sps30Result | None:
        params = self._params_or_warn()
        if params is None:
            return None
        from backend import sps30_workflow

        result = sps30_workflow.run_analytic(self.store, params)
        self._show(result)
        return result

    def prepare_cfd(self) -> None:
        params = self._params_or_warn()
        if params is None:
            return
        cfd = self.cfd_settings()
        from backend import sps30_workflow

        def done(prepared: dict) -> None:
            self.refresh_studies(select=prepared["study_id"])
            self.append_log(
                f"Prepared {prepared['design_points']} design points on {prepared['cell_count']:,} cells.\n"
                f"Fluent package: {prepared['fluent_folder']}\n"
                f"Solve here, or run {Path(prepared['run_script_windows']).name} on the solving computer."
            )

        self._start(lambda progress: sps30_workflow.prepare_cfd(self.store, params, cfd, progress),
                    done, "Preparing the CFD cases and the Fluent package ...")

    def solve_cfd(self) -> None:
        study = self.study_list.currentData()
        if not study:
            QtWidgets.QMessageBox.information(self, "SPS30 housing study", "Prepare the CFD cases first.")
            return
        from backend import sps30_workflow
        from backend.runner import SU2Runner

        limit = self.solve_limit.value() or None

        def done(summary: dict) -> None:
            for failure in summary.get("failures", []):
                self.append_log(f"FAILED {failure}")
            self.append_log(f"{summary['solved']} of {summary['total']} design points solved")
            if summary.get("result") is not None:
                self._show(summary["result"])
            elif summary.get("analysis_pending"):
                self.append_log(summary["analysis_pending"])

        self._start(lambda progress: sps30_workflow.solve_cfd(self.store, study, SU2Runner(), progress, limit),
                    done, f"Solving the design points of {study} ...")

    def import_csv(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Solved design points", "", "CSV files (*.csv)")
        if not path:
            return
        params = self._params_or_warn()
        if params is None:
            return
        from backend import sps30_workflow

        try:
            self._show(sps30_workflow.analyse_imported(self.store, params, path))
        except Exception as error:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Import failed", str(error))

    def open_folder(self) -> None:
        study = self.study_list.currentData()
        if study:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.store.get(study).directory)))

    # ------------------------------------------------------------- results

    def refresh_studies(self, select: str | None = None) -> None:
        current = select or self.study_list.currentData()
        self.study_list.blockSignals(True)
        self.study_list.clear()
        for record in self.store.list_records("sps30"):
            text = f"{record.record_id}  ({record.metadata.get('label', '')})"
            if record.metadata.get("reliability") is not None:
                text += f"  reliability {100 * record.metadata['reliability']:.1f} %"
            self.study_list.addItem(text, record.record_id)
        index = self.study_list.findData(current) if current else -1
        self.study_list.setCurrentIndex(index if index >= 0 else (0 if self.study_list.count() else -1))
        self.study_list.blockSignals(False)

    def _study_selected(self, *_: Any) -> None:
        study = self.study_list.currentData()
        if not study:
            return
        from backend import sps30_workflow

        try:
            self.apply_params(sps30_workflow.load_params(self.store, study))
            result = sps30_workflow.load_result(self.store, study)
        except Exception as error:  # noqa: BLE001
            self.append_log(f"Could not open {study}: {error}")
            return
        if result is not None:
            self._show(result, refresh=False)

    def _show(self, result: Sps30Result, refresh: bool = True) -> None:
        self._result = result
        o = result.outputs
        self.card_face.set_value(o["face_velocity_ms"].maximum, "{:.3f}")
        self.card_pen.set_value(100.0 * o["penetration"].maximum, "{:.2f}")
        self.card_flow.set_value(o["exchange_flow_lpm"].minimum, "{:.2f}")
        self.card_reliability.set_value(100.0 * result.reliability, "{:.1f}")
        goal = result.params.setup.max_face_velocity_ms
        self.card_face.set_colour(SUCCESS if o["face_velocity_ms"].maximum <= goal else DANGER)
        colour = SUCCESS if result.reliability >= 0.95 else (WARNING if result.reliability >= 0.8 else DANGER)
        self.card_reliability.set_colour(colour)
        lines = [
            f"{result.evaluator.value} design points: {len(result.points)}; {result.samples:,} Monte Carlo samples.",
            "Goals met: " + ", ".join(f"{k.replace('_', ' ')} {100 * v:.1f} %" for k, v in result.reliability_by_goal.items()),
        ]
        for name in SPS30_OUTPUTS:
            s = o[name]
            w = s.worst_case
            lines.append(
                f"{SPS30_OUTPUT_LABELS[name]}: worst {w[name]:.4g} at {w['speed_ms']:.1f} m/s, "
                f"yaw {w['yaw_deg']:+.1f} deg, {w['droplet_um']:.0f} um (surface LOO {s.loo_rmse:.3g})"
            )
        if result.first_failure:
            f = result.first_failure
            lines.append(
                f"Worst failing condition: {f['speed_ms']:.1f} m/s, yaw {f['yaw_deg']:+.1f} deg, "
                f"{f['droplet_um']:.0f} um -> face {f['face_velocity_ms']:.3f} m/s, penetration {100 * f['penetration']:.2f} %"
            )
        lines += result.notes
        self.summary.setText("\n".join(lines))
        self.points_table.setRowCount(len(result.points))
        for r, p in enumerate(result.points):
            values = [p.name, f"{p.speed_ms:.3g}", f"{p.yaw_deg:+.3g}", f"{p.droplet_um:.4g}",
                      "" if p.face_velocity_ms is None else f"{p.face_velocity_ms:.4f}",
                      "" if p.penetration is None else f"{p.penetration:.4g}",
                      "" if p.exchange_flow_lpm is None else f"{p.exchange_flow_lpm:.3f}"]
            for c, v in enumerate(values):
                self.points_table.setItem(r, c, QtWidgets.QTableWidgetItem(v))
        self._draw()
        if self.isVisible():
            from backend.study_preview import preview_key

            if getattr(self.geometry_view, "key", None) != preview_key("sps30", result.params.setup):
                self._draw_geometry_for(result.params.setup)
        if refresh and result.study_id:
            self.refresh_studies(select=result.study_id)
        self.statusMessage.emit(f"SPS30 study: reliability {100 * result.reliability:.1f} %")

    def _draw(self) -> None:
        if self.histogram is None or self._result is None:
            return
        import numpy as np

        name = self.chart_output.currentData()
        s = self._result.outputs[name]
        self.histogram.clear()
        edges = np.array(s.histogram_edges)
        self.histogram.addItem(pg.BarGraphItem(x0=edges[:-1], x1=edges[1:], height=s.histogram_counts, brush=CHART_COLOURS[0]))
        self.histogram.setLabel("bottom", SPS30_OUTPUT_LABELS[name])
        setup = self._result.params.setup
        limit = {"face_velocity_ms": setup.max_face_velocity_ms, "penetration": setup.max_penetration,
                 "exchange_flow_lpm": setup.min_exchange_flow_lpm or None}[name]
        if limit is not None:
            self.histogram.addItem(pg.InfiniteLine(limit, pen=pg.mkPen(DANGER, style=QtCore.Qt.PenStyle.DashLine)))
