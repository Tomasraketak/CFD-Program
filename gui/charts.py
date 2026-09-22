"""Live convergence chart, shared by the Aerodynamics tab and the assistant.

The solver prints one line per iteration; this plots the density residual and
the force coefficients as they arrive, and says how fast the solve is going.
A residual falling steadily is a solve that is working; one that flattens or
climbs is one to stop, and the operator should be able to see that at a
glance without reading the log.
"""

from __future__ import annotations

import math
import time
from collections import deque

from PySide6 import QtCore, QtWidgets

from gui.theme import CHART_COLOURS

try:  # pragma: no cover - import guard
    import pyqtgraph as pg

    HAVE_CHARTS = True
except Exception:  # pragma: no cover - optional
    pg = None  # type: ignore[assignment]
    HAVE_CHARTS = False

# Iterations averaged over when quoting the current speed. Long enough to
# smooth over a slow iteration that wrote a restart file, short enough to
# follow the solver slowing down as the CFL ramps.
RATE_WINDOW = 20

# How many iterations the residual trend is judged over.
TREND_WINDOW = 50


def format_duration(seconds: float) -> str:
    """'42 s', '3:05' or '1:02:09' -- short enough for a status line."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}:{secs:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


class ResidualChart(QtWidgets.QWidget):
    """Live convergence plot of residuals and force coefficients."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self._iterations: list[int] = []
        self._series: dict[str, list[float]] = {}
        self._stamps: deque[float] = deque(maxlen=RATE_WINDOW + 1)
        # A rescued run is several solver invocations, each counting its own
        # iterations from 1. Without an offset the trace folds back on itself
        # and the chart looks broken.
        self._stage_offset = 0
        self._last_iteration = 0

        # One line under the plot: iteration, speed, and whether it is
        # converging -- the three things worth knowing mid-solve.
        self.status = QtWidgets.QLabel("Waiting for the solver …")
        self.status.setObjectName("hint")

        if not HAVE_CHARTS:  # pragma: no cover - optional dependency
            placeholder = QtWidgets.QLabel(
                "Install pyqtgraph to see live convergence plots"
            )
            placeholder.setObjectName("hint")
            placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(placeholder)
            layout.addWidget(self.status)
            self.plot = None
            self.curves = {}
            return

        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("#12151c")
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setLabel("bottom", "Iteration")
        self.plot.setLabel("left", "log10 residual / coefficient")
        self.plot.addLegend(offset=(-10, 10))
        layout.addWidget(self.plot, 1)
        layout.addWidget(self.status)

        self.curves = {
            name: self.plot.plot(
                pen=pg.mkPen(CHART_COLOURS[index % len(CHART_COLOURS)], width=2),
                name=label,
            )
            for index, (name, label) in enumerate(
                (("rms_rho", "RMS[Rho]"), ("cd", "C_d"), ("cl", "C_l"))
            )
        }

    def clear(self) -> None:
        """Reset the chart for a new run."""
        self._iterations.clear()
        self._series.clear()
        self._stamps.clear()
        self._stage_offset = 0
        self._last_iteration = 0
        self.status.setText("Waiting for the solver …")
        for curve in self.curves.values():
            curve.setData([], [])

    def begin_stage(self) -> None:
        """Continue the x axis into a new solver stage.

        Called when a diverged run is retried: the solver restarts its
        iteration counter, but the operator is watching one continuous solve.
        """
        self._stage_offset = self._last_iteration

    @property
    def iteration_count(self) -> int:
        """Iterations plotted so far."""
        return len(self._iterations)

    def iterations_per_second(self) -> float | None:
        """Current solver speed over the last few iterations."""
        if len(self._stamps) < 2:
            return None
        elapsed = self._stamps[-1] - self._stamps[0]
        if elapsed <= 0.0:
            return None
        return (len(self._stamps) - 1) / elapsed

    def residual_trend(self) -> float | None:
        """Change in log10 RMS[Rho] per 100 iterations, over the recent window.

        Negative is converging. A slope near zero is a stall; positive is a
        solve heading the wrong way.
        """
        series = self._series.get("rms_rho", [])
        points = [
            (i, v)
            for i, v in zip(self._iterations[-TREND_WINDOW:], series[-TREND_WINDOW:])
            if v is not None and math.isfinite(v)
        ]
        if len(points) < 10:
            return None
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        spread = sum((x - mean_x) ** 2 for x in xs)
        if spread <= 0.0:
            return None
        slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / spread
        return slope * 100.0

    def add_record(self, record, now: float | None = None) -> None:
        """Append one solver iteration."""
        self._stamps.append(time.monotonic() if now is None else now)
        iteration = record.iteration + self._stage_offset
        self._last_iteration = iteration
        self._iterations.append(iteration)
        for name in ("rms_rho", "cd", "cl"):
            value = record.get(name)
            self._series.setdefault(name, []).append(
                value if value is not None else math.nan
            )
        for name, curve in self.curves.items():
            series = self._series[name]
            finite = [
                (i, v)
                for i, v in zip(self._iterations, series)
                if v is not None and math.isfinite(v)
            ]
            if finite:
                curve.setData([i for i, _ in finite], [v for _, v in finite])
        self._update_status()

    def _update_status(self) -> None:
        """Describe the solve in one line."""
        parts = [f"Iteration {self._last_iteration}"]
        rate = self.iterations_per_second()
        if rate is not None:
            parts.append(f"{rate:.2f} it/s")
        residual = self._series.get("rms_rho", [math.nan])[-1]
        if residual is not None and math.isfinite(residual):
            parts.append(f"RMS[Rho] {residual:.2f}")
        trend = self.residual_trend()
        if trend is not None:
            if trend < -0.05:
                verdict = f"converging ({trend:+.2f} per 100 it)"
            elif trend > 0.05:
                verdict = f"diverging ({trend:+.2f} per 100 it)"
            else:
                verdict = "stalled — forces may still settle"
            parts.append(verdict)
        self.status.setText("  ·  ".join(parts))
