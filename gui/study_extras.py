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
