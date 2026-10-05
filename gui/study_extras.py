"""Sweep and geometry-picture pieces shared by the sensor-study panels.

Both the radiation-shield and the SPS30 panel get the same two extras:

* a **sweep** -- one input stepped from a start to a stop value, every
  point solved directly with the analytical model, shown as a chart and a
  table (the ``sweep`` action of the assistant's tools);
* a **geometry picture** -- the shield or housing the study meshes, drawn
  with its loads (the ``preview`` action).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from PySide6 import QtCore, QtGui, QtWidgets

from gui.theme import CHART_COLOURS

try:  # pragma: no cover - import guard
    import pyqtgraph as pg

    HAVE_CHARTS = True
except Exception:  # pragma: no cover - optional
    pg = None  # type: ignore[assignment]
    HAVE_CHARTS = False


class SweepControls(QtWidgets.QGroupBox):
    """Which input to sweep, from where to where, in what step."""

    def __init__(
        self,
        inputs: dict[str, str],
        defaults: dict[str, tuple[float, float, float]],
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__("Sweep one input (analytical model, fixed steps)", parent)
        self.defaults = defaults
        form = QtWidgets.QFormLayout(self)
        self.variable = QtWidgets.QComboBox()
        for name, label in inputs.items():
            self.variable.addItem(label, name)
        form.addRow("Input", self.variable)
        self.start = self._spin()
        self.stop = self._spin()
        self.step = self._spin()
        self.step.setMinimum(1e-6)
        form.addRow("From", self.start)
        form.addRow("To", self.stop)
        form.addRow("Step", self.step)
        hint = QtWidgets.QLabel(
            "The other inputs stay at the baseline values above. Every point is "
            "solved directly, nothing is read off the response surface."
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        form.addRow(hint)
        buttons = QtWidgets.QHBoxLayout()
        self.run_button = QtWidgets.QPushButton("Run sweep")
        self.preview_button = QtWidgets.QPushButton("Draw 3D geometry")
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.preview_button)
        form.addRow(buttons)
        self.variable.currentIndexChanged.connect(self._variable_changed)
        self._variable_changed()

    @staticmethod
    def _spin() -> QtWidgets.QDoubleSpinBox:
        box = QtWidgets.QDoubleSpinBox()
        box.setRange(-1e6, 1e6)
        box.setDecimals(3)
        return box

    def _variable_changed(self, *_: Any) -> None:
        start, stop, step = self.defaults[self.variable.currentData()]
        self.start.setValue(start)
        self.stop.setValue(stop)
        self.step.setValue(step)
        self.start.setSingleStep(step)
        self.stop.setSingleStep(step)

    def values(self) -> tuple[str, float, float, float]:
        return (
            self.variable.currentData(),
            self.start.value(),
            self.stop.value(),
            self.step.value(),
        )

    def set_values(self, variable: str, start: float, stop: float, step: float) -> None:
        index = self.variable.findData(variable)
        if index >= 0:
            self.variable.setCurrentIndex(index)
        self.start.setValue(start)
        self.stop.setValue(stop)
        self.step.setValue(step)


class SweepView(QtWidgets.QWidget):
    """The last sweep: one output against the swept input, and every row."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.result = None
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Chart"))
        self.output = QtWidgets.QComboBox()
        self.output.currentIndexChanged.connect(lambda *_: self._draw())
        row.addWidget(self.output, 1)
        self.info = QtWidgets.QLabel("Run a sweep to see it here.")
        self.info.setObjectName("hint")
        row.addWidget(self.info, 2)
        layout.addLayout(row)
        if HAVE_CHARTS:
            self.chart = pg.PlotWidget()
            # The labels carry the units; an automatic "m" prefix would turn
            # -0.86 K into an axis reading -860.
            for side in ("left", "bottom"):
                self.chart.getAxis(side).enableAutoSIPrefix(False)
            layout.addWidget(self.chart, 2)
        else:  # pragma: no cover
            self.chart = None
        self.table = QtWidgets.QTableWidget(0, 0)
        layout.addWidget(self.table, 2)

    def show_result(self, result) -> None:
        from backend.parameter_sweep import INPUT_LABELS

        self.result = result
        self.output.blockSignals(True)
        self.output.clear()
        for name, label in result.outputs.items():
            self.output.addItem(label, name)
        self.output.blockSignals(False)
        columns = list(result.rows[0].keys())
        labels = {**INPUT_LABELS, **result.outputs}
        self.table.clear()
        self.table.setColumnCount(len(columns))
        self.table.setRowCount(len(result.rows))
        self.table.setHorizontalHeaderLabels([labels.get(c, c) for c in columns])
        for r, row in enumerate(result.rows):
            for c, name in enumerate(columns):
                value = row[name]
                text = f"{value:.4g}" if name in INPUT_LABELS else f"{value:.5g}"
                self.table.setItem(r, c, QtWidgets.QTableWidgetItem(text))
        self.table.resizeColumnsToContents()
        self.info.setText(
            f"{len(result.rows)} points; CSV: {result.csv_path}" if result.csv_path
            else f"{len(result.rows)} points"
        )
        self._draw()

    def _draw(self) -> None:
        if self.chart is None or self.result is None:
            return
        from backend.parameter_sweep import INPUT_LABELS

        name = self.output.currentData() or next(iter(self.result.outputs))
        self.chart.clear()
        self.chart.setLabel("bottom", INPUT_LABELS[self.result.variable])
        self.chart.setLabel("left", self.result.outputs[name])
        self.chart.plot(
            self.result.column(self.result.variable), self.result.column(name),
            pen=pg.mkPen(CHART_COLOURS[0], width=2), symbol="o", symbolSize=6,
            symbolBrush=CHART_COLOURS[0],
        )


class GeometryView(QtWidgets.QScrollArea):
    """The study geometry drawn with its loads."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.image = QtWidgets.QLabel(
            "Press 'Draw 3D geometry' (or run a study) to see the geometry."
        )
        self.image.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setWidget(self.image)
        self.path: Path | None = None

    def show_image(self, path: Path | str) -> None:
        self.path = Path(path)
        pixmap = QtGui.QPixmap(str(path))
        if pixmap.isNull():
            self.image.setText(f"Could not read {path}")
            return
        width = max(self.viewport().width() - 8, 600)
        self.image.setPixmap(
            pixmap.scaledToWidth(min(width, pixmap.width()),
                                 QtCore.Qt.TransformationMode.SmoothTransformation)
        )


def results_tabs(points_table: QtWidgets.QWidget) -> tuple[QtWidgets.QTabWidget, SweepView, GeometryView]:
    """Design points, sweep and geometry as tabs under a panel's charts."""
    tabs = QtWidgets.QTabWidget()
    tabs.addTab(points_table, "Design points")
    sweep = SweepView()
    tabs.addTab(sweep, "Sweep")
    geometry = GeometryView()
    tabs.addTab(geometry, "3D geometry")
    return tabs, sweep, geometry


def run_in_background(panel, function: Callable[[], Any], done: Callable[[Any], None],
                      label: str) -> None:
    """Run a short job off the GUI thread without locking the panel's buttons."""
    from gui.workers import TaskWorker

    worker = TaskWorker(lambda progress: function())
    worker.signals.finished.connect(done)
    worker.signals.failed.connect(panel.append_log)
    panel._workers.append(worker)
    panel.append_log(label)
    panel.pool.start(worker)


def publish_study_state(panel, kind: str, delay_ms: int = 400) -> QtCore.QTimer:
    """Keep the assistant told what this study tab shows.

    Every edit of the form (re)starts a short timer; when it fires the
    tab's current study settings are written where the assistant's tools
    read them, so "the shield I have open" is the shield on this tab.
    """
    from PySide6 import QtWidgets as W

    from core.workspace import set_active_study

    timer = QtCore.QTimer(panel)
    timer.setSingleShot(True)
    timer.setInterval(delay_ms)

    def publish() -> None:
        try:
            params = panel.study_params()
        except Exception:  # noqa: BLE001 - the form is mid-edit
            return
        try:
            set_active_study(kind, params.model_dump(mode="json"))
        except OSError:  # pragma: no cover - read-only data folder
            pass

    timer.timeout.connect(publish)
    for widget in panel.findChildren(W.QWidget):
        if isinstance(widget, W.QLineEdit):
            widget.textChanged.connect(timer.start)
        elif isinstance(widget, W.QAbstractSpinBox) and hasattr(widget, "valueChanged"):
            widget.valueChanged.connect(timer.start)
        elif isinstance(widget, W.QComboBox):
            widget.currentIndexChanged.connect(timer.start)
        elif isinstance(widget, W.QCheckBox):
            widget.toggled.connect(timer.start)
    panel.publish_study_state = publish
    publish()
    return timer


class CfdPointTools(QtWidgets.QGroupBox):
    """Draw a solved CFD point, or add extra points to verify the surface.

    Works on the study selected in the results list (one prepared with
    'Prepare CFD cases'); the same as the assistant's 'render' and
    'add_points' actions.
    """

    def __init__(self, inputs: dict[str, tuple[str, float, float, float]],
                 parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__("CFD points: render the air, add points to verify", parent)
        form = QtWidgets.QFormLayout(self)
        self.point = QtWidgets.QComboBox()
        self.point.setEditable(True)
        self.point.setToolTip("A solved point of the selected study (DP0, DP7, X1, ...)")
        form.addRow("Point", self.point)
        self.quantity = QtWidgets.QComboBox()
        for label, value in (
            ("Temperature + velocity", "temperature,velocity"),
            ("Air temperature", "temperature"), ("Air speed", "velocity"),
            ("Streamlines", "streamlines"), ("Wall temperature", "wall_temperature"),
            ("All four", "all"),
        ):
            self.quantity.addItem(label, value)
        form.addRow("Show", self.quantity)
        self.plane = QtWidgets.QComboBox()
        for label, value in (("Along the wind (y)", "y"), ("Across the wind (x)", "x"),
                             ("Horizontal (z)", "z")):
            self.plane.addItem(label, value)
        form.addRow("Cutting plane", self.plane)
        self.render_button = QtWidgets.QPushButton("Render CFD point")
        form.addRow(self.render_button)
        self.inputs: dict[str, QtWidgets.QDoubleSpinBox] = {}
        row = QtWidgets.QHBoxLayout()
        for name, (label, low, high, value) in inputs.items():
            box = QtWidgets.QDoubleSpinBox()
            box.setRange(low, high)
            box.setDecimals(3)
            box.setValue(value)
            box.setToolTip(label)
            self.inputs[name] = box
            row.addWidget(box)
        holder = QtWidgets.QWidget()
        holder.setLayout(row)
        form.addRow("New point " + " / ".join(label.split(" [")[0] for label, *_ in inputs.values()),
                    holder)
        buttons = QtWidgets.QHBoxLayout()
        self.add_button = QtWidgets.QPushButton("Add point")
        self.worst_button = QtWidgets.QPushButton("Add the worst case")
        self.worst_button.setToolTip(
            "Adds the Monte Carlo worst case as a CFD point, to check the response "
            "surface where it is least accurate. Solve it with 'Solve CFD design points'."
        )
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.worst_button)
        holder = QtWidgets.QWidget()
        holder.setLayout(buttons)
        form.addRow(holder)

    def set_points(self, names: list[str]) -> None:
        current = self.point.currentText()
        self.point.blockSignals(True)
        self.point.clear()
        self.point.addItems(names)
        if current in names:
            self.point.setCurrentText(current)
        self.point.blockSignals(False)

    def condition(self) -> dict[str, float]:
        return {name: box.value() for name, box in self.inputs.items()}


def wire_cfd_tools(panel, kind: str, tools: CfdPointTools) -> None:
    """Connect the CFD point tools of a study panel to its workflow."""
    import importlib

    workflow = importlib.import_module(
        "backend.shield_workflow" if kind == "shield" else "backend.sps30_workflow"
    )
    condition_model = importlib.import_module(
        "core.shield_models" if kind == "shield" else "core.sps30_models"
    )
    condition_class = getattr(condition_model, "ShieldCondition" if kind == "shield" else "Sps30Condition")

    def study() -> str | None:
        return panel.study_list.currentData()

    def refresh_points(*_):
        name = study()
        names = []
        if name:
            try:
                names = [r["name"] for r in workflow.point_reports(panel.store, name)]
            except Exception:  # noqa: BLE001 - not a CFD study
                names = []
        tools.set_points(names)

    def render():
        name = study()
        point = tools.point.currentText().strip()
        if not name or not point:
            QtWidgets.QMessageBox.information(panel, "Render", "Select a CFD study and a solved point.")
            return
        from backend.study_render import QUANTITIES, render_study_point

        quantities = tools.quantity.currentData()
        quantities = list(QUANTITIES) if quantities == "all" else quantities.split(",")
        plane = tools.plane.currentData()
        folder = panel.store.get(name).path("cfd")

        def job():
            return [
                str(render_study_point(folder, kind, point, q,
                                       panel.store.get(name).path("renders", f"{point}_{q}_{plane}.png"),
                                       plane))
                for q in quantities
            ]

        def done(paths):
            if paths:
                panel.geometry_view.show_image(paths[0])
                panel.result_tabs.setCurrentWidget(panel.geometry_view)
                for path in paths:
                    panel.append_log(f"Rendered {path}")

        run_in_background(panel, job, done, f"Rendering {point} ...")

    def add(worst: bool):
        name = study()
        if not name:
            QtWidgets.QMessageBox.information(panel, "Add point", "Select a prepared CFD study first.")
            return
        try:
            conditions = [] if worst else [condition_class(**tools.condition())]
            added = workflow.add_cfd_points(panel.store, name, conditions, worst)
        except Exception as error:  # noqa: BLE001 - shown to the operator
            QtWidgets.QMessageBox.warning(panel, "Add point", str(error))
            return
        panel.append_log(
            "Added " + ", ".join(p.name for p in added)
            + " -- solve them with 'Solve CFD design points'."
        )

    panel.study_list.currentIndexChanged.connect(refresh_points)
    tools.render_button.clicked.connect(render)
    tools.add_button.clicked.connect(lambda: add(False))
    tools.worst_button.clicked.connect(lambda: add(True))
    panel.refresh_cfd_points = refresh_points
    refresh_points()
