"""AeroThermalStudio desktop application.

Two tabs, matching the two simulation tracks: rocket aerodynamics with fin
hinge torque, and the BMP580 sensor microclimate. Both drive the same backend
the MCP server drives, through the same parameter models, so an agent and an
operator can hand work back and forth through the shared run registry.

The 3D viewport is embedded PyVista when ``pyvistaqt`` is available and
degrades to a placeholder panel when it is not, so the application still
starts on a machine with no working GL stack.
"""

from __future__ import annotations

import math
import sys
import time
from pathlib import Path
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets

from core.models import (
    AeroRunRequest,
    AxisDirection,
    BodyKind,
    ConvectiveScheme,
    DomainParams,
    DomainShape,
    FlowParams,
    GeometryParams,
    HingeAxis,
    MeshParams,
    MeshRequest,
    MeshResolution,
    SimulationTrack,
    SolverParams,
    ThermalParams,
    VelocityType,
)
from core.frames import AXIS_CONVENTION, Frame
from core.platform_env import probe_environment, recommended_mpi_ranks
from core.project import (
    PROJECT_FILTER,
    Project,
    ProjectError,
    SweepSettings,
    default_project,
    projects_directory,
)
from core.settings import AppSettings, load_settings, save_settings
from core.workspace import clear_active_geometry, set_active_geometry
from core.store import RunStore, default_store
from gui.ai_panel import AITab
from gui.charts import ResidualChart, format_duration  # noqa: F401
from gui.gallery import GraphicsTab, find_rendered_images
from gui.form_builder import LabelledSlider
from gui.theme import (
    ACCENT,
    CHART_COLOURS,
    DANGER,
    STYLESHEET,
    SUCCESS,
    TEXT_MUTED,
    WARNING,
)
from gui.workers import (
    AeroWorker,
    GeometryPreviewWorker,
    MeshWorker,
    SweepWorker,
    ThermalWorker,
)

APP_NAME = "AeroThermalStudio"

# How often the clock beside the progress bar redraws. The step itself only
# changes when the worker reports one; this is purely so the seconds move.
STEP_CLOCK_INTERVAL_MS = 500

# CAD beyond this is not worth tessellating just to look at; the import
# itself would take longer than the operator's patience.
MAX_PREVIEW_BYTES = 150 * 1024 * 1024

# Optional viewport and charting dependencies, both degraded gracefully.
try:  # pragma: no cover - import guard
    from pyvistaqt import QtInteractor

    HAVE_VIEWPORT = True
except Exception:  # pragma: no cover - optional
    QtInteractor = None  # type: ignore[assignment]
    HAVE_VIEWPORT = False

try:  # pragma: no cover - import guard
    import pyqtgraph as pg

    HAVE_CHARTS = True
except Exception:  # pragma: no cover - optional
    pg = None  # type: ignore[assignment]
    HAVE_CHARTS = False


# ---------------------------------------------------------------------------
# Small reusable widgets
# ---------------------------------------------------------------------------


class ResultCard(QtWidgets.QFrame):
    """A large numeric readout with a caption and unit."""

    def __init__(
        self, caption: str, unit: str = "", parent: QtWidgets.QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)

        self.caption = QtWidgets.QLabel(caption)
        self.caption.setObjectName("hint")
        self.value = QtWidgets.QLabel("--")
        self.value.setObjectName("resultValue")
        self.unit = QtWidgets.QLabel(unit)
        self.unit.setObjectName("resultUnit")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(1)
        layout.addWidget(self.caption)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(self.value)
        row.addWidget(self.unit, 0, QtCore.Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)
        layout.addLayout(row)

    def set_value(self, value: float | str, fmt: str = "{:.4g}") -> None:
        """Update the displayed number."""
        if isinstance(value, str):
            self.value.setText(value)
        elif value is None or (isinstance(value, float) and math.isnan(value)):
            self.value.setText("--")
        else:
            self.value.setText(fmt.format(value))

    def set_colour(self, colour: str) -> None:
        """Tint the number, used to flag out-of-range results."""
        self.value.setStyleSheet(f"color: {colour};")


class VectorInput(QtWidgets.QWidget):
    """Three spin boxes forming an [x, y, z] vector."""

    valueChanged = QtCore.Signal()

    def __init__(
        self,
        value: tuple[float, float, float] = (0.0, 0.0, 0.0),
        decimals: int = 4,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.boxes: list[QtWidgets.QDoubleSpinBox] = []
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        for index, label in enumerate("XYZ"):
            caption = QtWidgets.QLabel(label)
            caption.setObjectName("hint")
            box = QtWidgets.QDoubleSpinBox()
            box.setDecimals(decimals)
            box.setRange(-1.0e6, 1.0e6)
            box.setSingleStep(0.01)
            box.setValue(value[index])
            box.valueChanged.connect(lambda *_: self.valueChanged.emit())
            self.boxes.append(box)
            layout.addWidget(caption)
            layout.addWidget(box, 1)

    def value(self) -> list[float]:
        """The vector as a list."""
        return [box.value() for box in self.boxes]

    def set_value(self, value: tuple[float, float, float]) -> None:
        """Set all three components."""
        for box, component in zip(self.boxes, value):
            box.setValue(float(component))


class Viewport(QtWidgets.QWidget):
    """Embedded 3D scene with orbit, pan, zoom and a slice control."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.interactor = None
        if HAVE_VIEWPORT:  # pragma: no cover - needs a GL context
            try:
                self.interactor = QtInteractor(self)
                self.interactor.set_background("#12151c", top="#1a1f29")
                layout.addWidget(self.interactor.interactor, 1)
            except Exception:
                self.interactor = None

        if self.interactor is None:
            placeholder = QtWidgets.QLabel(
                "3D viewport unavailable.\n"
                "Install pyvistaqt and a working OpenGL driver to enable it."
            )
            placeholder.setObjectName("hint")
            placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            placeholder.setMinimumHeight(320)
            layout.addWidget(placeholder, 1)

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(8)

        self.colormap = QtWidgets.QComboBox()
        self.colormap.addItems(["turbo", "coolwarm", "viridis", "plasma", "inferno"])
        self.view = QtWidgets.QComboBox()
        self.view.addItems(
            [
                "isometric", "front", "back", "side", "top", "bottom",
                "nose_quarter", "tail_quarter",
            ]
        )
        self.view.setToolTip(
            "Views of the rocket standing nose-up along +Z: 'front' looks at "
            "the nose from ahead, 'side' at the pitch plane."
        )
        self.slice_slider = LabelledSlider(0.0, 1.0, 0.5, decimals=2)
        self.slice_enabled = QtWidgets.QCheckBox("Slice")

        for label, widget in (
            ("Colormap", self.colormap),
            ("View", self.view),
        ):
            caption = QtWidgets.QLabel(label)
            caption.setObjectName("hint")
            controls.addWidget(caption)
            controls.addWidget(widget)
        controls.addWidget(self.slice_enabled)
        controls.addWidget(self.slice_slider, 1)
        layout.addLayout(controls)

        self.view.currentTextChanged.connect(self._apply_view)

    @property
    def available(self) -> bool:
        """True when a real 3D interactor is present."""
        return self.interactor is not None

    def show_mesh(self, mesh: Any, **kwargs: Any) -> None:
        """Display a dataset, replacing whatever was shown."""
        if self.interactor is None:
            return
        self.interactor.clear()
        self.interactor.add_mesh(mesh, **kwargs)
        # The triad is what makes "nose along +Z" checkable at a glance.
        try:
            self.interactor.add_axes()
        except Exception:  # pragma: no cover - rendering failure
            pass
        self.interactor.reset_camera()

    def set_overlay(self, items: list) -> None:
        """Replace the flow-and-axis overlay with new meshes."""
        if self.interactor is None:
            return
        for actor in getattr(self, "_overlay_actors", []):
            try:
                self.interactor.remove_actor(actor)
            except Exception:  # pragma: no cover - already gone
                pass
        self._overlay_actors = []
        for mesh, options in items:
            try:
                self._overlay_actors.append(self.interactor.add_mesh(mesh, **options))
            except Exception:  # pragma: no cover - rendering failure
                pass
        try:
            self.interactor.reset_camera()
            self.interactor.render()
        except Exception:  # pragma: no cover
            pass

    def add_line(self, start, end, colour: str = ACCENT, width: int = 4) -> None:
        """Draw a line, used for the fin hinge axis."""
        if self.interactor is None:
            return
        try:
            import pyvista as pv

            self.interactor.add_mesh(
                pv.Line(start, end), color=colour, line_width=width
            )
        except Exception:  # pragma: no cover - rendering failure
            pass

    def _apply_view(self, name: str) -> None:
        """Move the camera to a named viewpoint."""
        if self.interactor is None:
            return
        try:
            # The viewport only ever shows the rocket frame: the imported
            # model and the hinge axes are both drawn nose-up.
            from backend.visualizer import ROCKET_CAMERA_VIEWS as views

            direction, up = views.get(name, views["isometric"])
            self.interactor.view_vector(direction, up)
        except Exception:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# Tab 1: aerodynamics and fins
# ---------------------------------------------------------------------------


class AerodynamicsTab(QtWidgets.QWidget):
    """Rocket aerodynamics: geometry, domain, flight condition, fins."""

    statusMessage = QtCore.Signal(str)
    # A solve finished; carries its sim_id so its pictures can be drawn.
    simulationFinished = QtCore.Signal(str)

    def __init__(
        self, store: RunStore, parent: QtWidgets.QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.mesh_id: str | None = None
        self.mesh_result: Any = None
        self.pool = QtCore.QThreadPool.globalInstance()
        self._worker: Any = None
        self._step_text = ""
        self._step_started: float | None = None
        self._operation_started: float | None = None
        self._step_timer = QtCore.QTimer(self)
        self._step_timer.timeout.connect(self._refresh_step_clock)
        # Bumped on every import so a slow preview cannot paint the previous
        # rocket over the one the operator has just opened.
        self._preview_token = 0

        self.setAcceptDrops(True)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_controls())
        splitter.addWidget(self._build_display())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 900])

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(splitter)

    # -- construction ------------------------------------------------------

    def _build_controls(self) -> QtWidgets.QWidget:
        """The left-hand parameter panel."""
        panel = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        content = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(content)
        layout.setSpacing(10)

        layout.addWidget(self._geometry_group())
        layout.addWidget(self._domain_group())
        layout.addWidget(self._flight_group())
        layout.addWidget(self._hinge_group())
        layout.addWidget(self._mesh_group())
        layout.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        outer.addLayout(self._action_buttons())
        return panel

    def _geometry_group(self) -> QtWidgets.QGroupBox:
        """CAD file, nose direction and reference origin."""
        group = QtWidgets.QGroupBox("Geometry")
        layout = QtWidgets.QFormLayout(group)

        self.step_path = QtWidgets.QLineEdit()
        self.step_path.setPlaceholderText("Drag a .step file here, or browse ...")
        # Whatever puts a path in the box -- browsing, a drop, a project, the
        # sample generator -- goes through one handler, so the file is always
        # inspected and always handed to the assistant.
        self.step_path.editingFinished.connect(self._on_step_path_changed)
        browse = QtWidgets.QPushButton("Browse")
        browse.clicked.connect(self._browse_step)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.step_path, 1)
        row.addWidget(browse)
        layout.addRow("STEP file", _wrap(row))

        self.geometry_note = QtWidgets.QLabel("No CAD file loaded.")
        self.geometry_note.setObjectName("hint")
        self.geometry_note.setWordWrap(True)
        layout.addRow("", self.geometry_note)

        # Which way the nose points in the CAD file, which is what anyone
        # looking at the model would say. The meshing parameter is the
        # opposite -- the nose-to-tail direction -- and the buttons used to
        # show that, so a rocket with its nose at +Y lit up "-Y". The
        # conversion now happens here, once, out of the operator's sight.
        self.axis_buttons = QtWidgets.QButtonGroup(self)
        axis_row = QtWidgets.QHBoxLayout()
        axis_row.setSpacing(3)
        for index, direction in enumerate(AxisDirection):
            button = QtWidgets.QPushButton(direction.value)
            button.setObjectName("axis")
            button.setCheckable(True)
            button.setChecked(direction is AxisDirection.MINUS_X)
            button.setToolTip(
                f"The oncoming air arrives from {direction.value} in the CAD "
                "file: where a rocket's nose points, or which edge of a fin "
                "leads. Whatever this is, the model is shown and reported "
                "with that side up, along +Z."
            )
            button.toggled.connect(self._on_setup_changed)
            self.axis_buttons.addButton(button, index)
            axis_row.addWidget(button)
        layout.addRow("Air comes from", _wrap(axis_row))

        # Rocket or fin: read from the shape on import (one side over five
        # times the others is a rocket, a thin plate a fin), and overridable.
        self.body_kind = QtWidgets.QComboBox()
        self.body_kind.addItem("Rocket", "rocket")
        self.body_kind.addItem("Fin / wing", "fin")
        self.body_kind.setToolTip(
            "Set from the model's proportions when a file is opened. A fin is "
            "referenced to its chord and planform area instead of a body "
            "cross-section."
        )
        self.body_kind.currentIndexChanged.connect(self._on_setup_changed)
        layout.addRow("Model is a", self.body_kind)

        # What the model tilts about when the angle of attack changes.
        self.tilt_axis = QtWidgets.QComboBox()
        self.tilt_axis.addItem("Automatic", None)
        for direction in AxisDirection:
            self.tilt_axis.addItem(f"CAD {direction.value}", direction.value)
        self.tilt_axis.setToolTip(
            "The CAD axis the model tilts about for an angle of attack -- a "
            "fin's span or hinge line. It must be across the flow. Automatic: "
            "a fin tilts about its span, a rocket as its nose axis implies. "
            "Shown in orange in the 3D view."
        )
        self.tilt_axis.currentIndexChanged.connect(self._on_setup_changed)
        layout.addRow("Tilt about", self.tilt_axis)

        self.custom_nose = QtWidgets.QCheckBox("Use custom vector")
        self.nose_vector = VectorInput((-1.0, 0.0, 0.0))
        self.nose_vector.setToolTip(
            "Direction the nose points in the CAD file, for a model drawn "
            "along no principal axis"
        )
        self.nose_vector.setEnabled(False)
        self.custom_nose.toggled.connect(self.nose_vector.setEnabled)
        layout.addRow("", self.custom_nose)
        layout.addRow("Nose vector", self.nose_vector)

        frame_note = QtWidgets.QLabel(
            "Shown and reported in rocket axes: nose along +Z, origin at "
            "the reference origin."
        )
        frame_note.setObjectName("hint")
        frame_note.setWordWrap(True)
        layout.addRow("", frame_note)

        self.reference_origin = VectorInput((0.0, 0.0, 0.0))
        self.reference_origin.setToolTip(
            "Point moved to the tunnel origin, typically the nose tip"
        )
        layout.addRow("Reference origin", self.reference_origin)

        self.scale = QtWidgets.QDoubleSpinBox()
        self.scale.setDecimals(6)
        self.scale.setRange(1e-6, 1e6)
        self.scale.setValue(1.0)
        self.scale.setToolTip("CAD units to metres; use 0.001 for millimetres")
        layout.addRow("Scale to metres", self.scale)
        return group

    def _domain_group(self) -> QtWidgets.QGroupBox:
        """Farfield envelope multipliers."""
        group = QtWidgets.QGroupBox("Farfield domain")
        layout = QtWidgets.QFormLayout(group)

        self.domain_shape = QtWidgets.QComboBox()
        for shape in DomainShape:
            self.domain_shape.addItem(shape.value, shape)
        layout.addRow("Shape", self.domain_shape)

        self.upstream = LabelledSlider(1.0, 20.0, 5.0, decimals=1, suffix=" L")
        self.downstream = LabelledSlider(1.0, 30.0, 10.0, decimals=1, suffix=" L")
        self.radial = LabelledSlider(1.0, 20.0, 5.0, decimals=1, suffix=" L")
        for label, widget, tip in (
            ("Upstream", self.upstream, "Nose to inlet, in body lengths"),
            ("Downstream", self.downstream, "Base to outlet, in body lengths"),
            ("Radial", self.radial, "Axis to farfield, in body lengths"),
        ):
            widget.setToolTip(tip)
            layout.addRow(label, widget)
        return group

    def _flight_group(self) -> QtWidgets.QGroupBox:
        """Speed, attitude and altitude."""
        group = QtWidgets.QGroupBox("Flight condition")
        layout = QtWidgets.QFormLayout(group)

        self.velocity_type = QtWidgets.QComboBox()
        self.velocity_type.addItem("Mach number", VelocityType.MACH)
        self.velocity_type.addItem("True airspeed [m/s]", VelocityType.TAS)
        self.velocity_type.currentIndexChanged.connect(self._on_velocity_type)
        layout.addRow("Speed as", self.velocity_type)

        self.velocity = LabelledSlider(0.2, 3.5, 2.0, decimals=2)
        layout.addRow("Speed", self.velocity)

        self.aoa = LabelledSlider(-20.0, 20.0, 0.0, decimals=1, suffix=" deg")
        self.aoa.setToolTip("Angle of attack, within the +/-20 degree envelope")
        self.aoa.valueChanged.connect(self._update_orientation_preview)
        layout.addRow("Angle of attack", self.aoa)

        self.sideslip = LabelledSlider(-20.0, 20.0, 0.0, decimals=1, suffix=" deg")
        self.sideslip.valueChanged.connect(self._update_orientation_preview)
        layout.addRow("Sideslip", self.sideslip)

        self.altitude = QtWidgets.QDoubleSpinBox()
        self.altitude.setRange(-610.0, 32000.0)
        self.altitude.setSuffix(" m")
        self.altitude.setSingleStep(100.0)
        layout.addRow("Altitude", self.altitude)

        self.condition_label = QtWidgets.QLabel()
        self.condition_label.setObjectName("hint")
        self.condition_label.setWordWrap(True)
        layout.addRow("", self.condition_label)

        for widget in (self.velocity, self.altitude):
            signal = getattr(widget, "valueChanged", None)
            if signal is not None:
                signal.connect(self._update_condition_label)
        self._update_condition_label()
        return group

    def _hinge_group(self) -> QtWidgets.QGroupBox:
        """Interactive fin hinge axis definition."""
        group = QtWidgets.QGroupBox("Fin hinge axis")
        layout = QtWidgets.QFormLayout(group)

        self.hinge_name = QtWidgets.QLineEdit("fin_1")
        layout.addRow("Name", self.hinge_name)

        self.hinge_point = VectorInput((0.0, 0.05, -0.9))
        self.hinge_point.setToolTip(
            "A point on the hinge line, in metres, in rocket axes: nose along "
            "+Z, so a fin near the tail has a negative Z"
        )
        self.hinge_point.valueChanged.connect(self._draw_hinge)
        layout.addRow("Point", self.hinge_point)

        self.hinge_direction = VectorInput((0.0, 1.0, 0.0))
        self.hinge_direction.setToolTip(
            "Rotation axis direction; the reported torque is the moment about "
            "the point projected onto this axis"
        )
        self.hinge_direction.valueChanged.connect(self._draw_hinge)
        layout.addRow("Direction", self.hinge_direction)

        show = QtWidgets.QPushButton("Show axis in 3D")
        show.clicked.connect(self._draw_hinge)
        layout.addRow("", show)
        return group

    def _mesh_group(self) -> QtWidgets.QGroupBox:
        """Meshing and solver controls."""
        group = QtWidgets.QGroupBox("Mesh and solver")
        layout = QtWidgets.QFormLayout(group)

        self.resolution = QtWidgets.QComboBox()
        for resolution in MeshResolution:
            self.resolution.addItem(resolution.value, resolution)
        self.resolution.setCurrentIndex(1)
        layout.addRow("Resolution", self.resolution)

        self.layers = QtWidgets.QSpinBox()
        self.layers.setRange(5, 8)
        self.layers.setValue(7)
        layout.addRow("Prism layers", self.layers)

        self.target_yplus = QtWidgets.QDoubleSpinBox()
        self.target_yplus.setRange(30.0, 300.0)
        self.target_yplus.setValue(45.0)
        self.target_yplus.setToolTip("y+ at the first cell centroid")
        layout.addRow("Target y+", self.target_yplus)

        self.ranks = QtWidgets.QSpinBox()
        self.ranks.setRange(1, 64)
        self.ranks.setValue(max(1, recommended_mpi_ranks()))
        layout.addRow("MPI ranks", self.ranks)

        # Solver numerics. "Auto" everywhere means "chosen from the Mach
        # number", which is right for almost every case; these are here for
        # the case that is not, and so the interface can do what the
        # assistant can.
        self.scheme = QtWidgets.QComboBox()
        self.scheme.addItem("Auto (JST below Mach 0.8, Roe above)", None)
        for scheme in ConvectiveScheme:
            self.scheme.addItem(scheme.value, scheme.value)
        layout.addRow("Scheme", self.scheme)

        self.cfl_start = _auto_spin(0.05, 100.0, 2, 0.5)
        self.cfl_start.setToolTip(
            "Starting CFL. Auto: 5 below Mach 0.6, 2 up to Mach 1.2, 1 above."
        )
        layout.addRow("CFL start", self.cfl_start)
        self.cfl_growth = _auto_spin(1.0, 3.0, 2, 0.05)
        self.cfl_growth.setToolTip(
            "Growth per iteration of the adaptive CFL. Auto: 1.15 / 1.10 / "
            "1.05 by regime. Near 2 it doubles every iteration and blows up."
        )
        layout.addRow("CFL growth", self.cfl_growth)
        self.cfl_max = _auto_spin(0.5, 1000.0, 1, 5.0)
        self.cfl_max.setToolTip("Ceiling of the adaptive CFL. Auto: 100 / 50 / 25.")
        layout.addRow("CFL max", self.cfl_max)

        self.max_iterations = QtWidgets.QSpinBox()
        self.max_iterations.setRange(100, 100_000)
        self.max_iterations.setSingleStep(500)
        self.max_iterations.setValue(SolverParams().max_iterations)
        layout.addRow("Max iterations", self.max_iterations)

        self.sweep_values = QtWidgets.QLineEdit("0.5, 1.0, 1.5, 2.0, 2.5")
        self.sweep_values.setToolTip("Comma-separated values for the batch sweep")
        layout.addRow("Sweep values", self.sweep_values)

        self.sweep_parameter = QtWidgets.QComboBox()
        self.sweep_parameter.addItems(["mach", "aoa", "sideslip", "altitude"])
        layout.addRow("Sweep parameter", self.sweep_parameter)
        return group

    def _action_buttons(self) -> QtWidgets.QHBoxLayout:
        """Mesh, run and sweep buttons plus the progress bar."""
        layout = QtWidgets.QVBoxLayout()

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # What the worker is doing right now, with a clock on it. Meshing
        # spends minutes inside single Gmsh calls; without the seconds
        # ticking, a working program is indistinguishable from a hung one.
        self.step_label = QtWidgets.QLabel()
        self.step_label.setObjectName("hint")
        self.step_label.setWordWrap(True)
        self.step_label.setVisible(False)
        layout.addWidget(self.step_label)

        row = QtWidgets.QHBoxLayout()
        self.mesh_button = QtWidgets.QPushButton("Generate mesh")
        self.mesh_button.clicked.connect(self.generate_mesh)
        self.run_button = QtWidgets.QPushButton("Run simulation")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self.run_simulation)
        self.run_button.setEnabled(False)
        self.sweep_button = QtWidgets.QPushButton("Batch sweep")
        self.sweep_button.clicked.connect(self.run_sweep)
        self.sweep_button.setEnabled(False)
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)

        for button in (
            self.mesh_button, self.run_button, self.sweep_button, self.cancel_button
        ):
            row.addWidget(button)
        layout.addLayout(row)
        return layout

    def _build_display(self) -> QtWidgets.QWidget:
        """The right-hand viewport, chart, results and log."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.viewport = Viewport()
        layout.addWidget(self.viewport, 3)
        self.overlay_note = QtWidgets.QLabel(
            "Open a STEP file: the air (blue) and the tilt axis (orange) are "
            "drawn over the model."
        )
        self.overlay_note.setObjectName("hint")
        layout.addWidget(self.overlay_note)

        self.chart = ResidualChart()
        # Shown only while a solve is iterating; afterwards the space goes
        # back to the model and the results.
        self.chart.setVisible(False)
        layout.addWidget(self.chart, 2)

        cards = QtWidgets.QHBoxLayout()
        self.card_cd = ResultCard("Drag coefficient", "C_d")
        self.card_cl = ResultCard("Lift coefficient", "C_l")
        self.card_drag = ResultCard("Drag force", "N")
        self.card_lift = ResultCard("Lift force", "N")
        self.card_torque = ResultCard("Hinge torque", "N m")
        self.card_cop = ResultCard("Centre of pressure, Z", "m")
        self.card_cop.setToolTip(
            "Distance along the rocket axis from the reference origin; "
            "negative is behind the nose. Undefined at zero incidence."
        )
        for card in (
            self.card_cd, self.card_cl, self.card_drag,
            self.card_lift, self.card_torque, self.card_cop,
        ):
            cards.addWidget(card)
        layout.addLayout(cards)

        # The force along each of the rocket's own axes, which is what
        # "how much force in each axis" means to someone holding the rocket
        # nose-up -- not the solver's frame, where the body lies along X.
        axis_cards = QtWidgets.QHBoxLayout()
        self.card_fx = ResultCard("Force along rocket X", "N")
        self.card_fy = ResultCard("Force along rocket Y", "N")
        self.card_fz = ResultCard("Force along rocket Z (nose)", "N")
        for card in (self.card_fx, self.card_fy, self.card_fz):
            card.setToolTip(AXIS_CONVENTION)
            axis_cards.addWidget(card)
        layout.addLayout(axis_cards)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(120)
        layout.addWidget(self.log)
        return panel

    # -- behaviour ---------------------------------------------------------

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:  # noqa: N802
        """Accept dragged STEP files."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:  # noqa: N802
        """Load the first dropped STEP file."""
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() in (".step", ".stp"):
                self.step_path.setText(str(path))
                self._on_step_path_changed()
                break

    def _browse_step(self) -> None:
        """Open a file dialog for the CAD file."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select a STEP file", "", "STEP files (*.step *.stp)"
        )
        if path:
            self.step_path.setText(path)
            self._on_step_path_changed()

    def _on_step_path_changed(self, keep_scale: bool = False) -> None:
        """Inspect a newly chosen CAD file and publish it to the assistant.

        Two things happen here that the operator would otherwise have to do
        by hand. The file's declared length unit is read and the scale box
        set from it -- a millimetre model taken as metres is a kilometre-long
        rocket, and no later step would notice. And the choice is recorded
        where the AI assistant can see it, so "mesh the file I just imported"
        needs no path typed back in.

        ``keep_scale`` keeps the scale already in the form, for when a
        project supplies it: a saved study's unit is a decision, not a
        guess to be overwritten.
        """
        path = self.step_path.text().strip()
        if not path:
            self.geometry_note.setText("No CAD file loaded.")
            clear_active_geometry()
            return

        axes = list(AxisDirection)
        checked = self.axis_buttons.checkedId()
        nose_points_to = axes[checked].value if 0 <= checked < len(axes) else "-X"
        direction = _opposite_axis(nose_points_to)
        # On a fresh import the axis buttons still hold the last file's
        # answer, so they are not passed in: the shape of this model decides,
        # and the buttons follow. A project supplies its own.
        record = set_active_geometry(
            path,
            scale_to_meters=self.scale.value() if keep_scale else None,
            nose_direction=direction if keep_scale else None,
            source="gui",
        )
        if not record.exists():
            self.geometry_note.setText("That file is not on disk.")
            return

        if not keep_scale:
            self.scale.setValue(record.scale_to_meters)
            if record.nose_is_confident and not self.custom_nose.isChecked():
                self._check_nose_button(_opposite_axis(record.nose_direction))
            self._detected_pitch_axis = record.pitch_axis
            blocked = self.body_kind.blockSignals(True)
            self.body_kind.setCurrentIndex(max(0, self.body_kind.findData(record.body_kind)))
            self.body_kind.blockSignals(blocked)

        note = record.summary()
        # Anything the geometry could not settle is put to the operator here
        # rather than defaulted. Both of these fail silently: a wrong unit
        # scales the Reynolds number by a thousand, a reversed nose flies the
        # rocket tail-first, and neither produces an error anywhere.
        questions = record.open_questions()
        if questions:
            note = f"{note}. Please check: {' '.join(questions)}"
        self.geometry_note.setText(note)
        self.geometry_note.setProperty("warn", bool(questions))
        self.append_log(f"Loaded {Path(path).name}: {note}")
        self.statusMessage.emit(note)
        self._start_preview()

    def _auto_pitch_axis(self) -> str | None:
        """The tilt axis read from the shape, for a fin; none for a rocket."""
        if self.body_kind.currentData() != "fin":
            return None
        pitch = getattr(self, "_detected_pitch_axis", None)
        if pitch is None:
            return None
        # It has to lie across the flow; the shape's guess can disagree with
        # an air direction the operator changed.
        button = self.axis_buttons.checkedButton()
        if button and button.text()[1:] == pitch[1:]:
            return None
        return pitch

    def _on_setup_changed(self, *_: Any) -> None:
        """Air direction, model kind or tilt axis changed: redraw the model.

        The alignment depends on all three, so the preview is rebuilt
        rather than the arrows merely moved.
        """
        if getattr(self, "_restoring", False):
            return
        if self.step_path.text().strip():
            self._start_preview()

    def _refresh_overlay(self, *_: Any) -> None:
        """Draw the oncoming air and the tilt axis over the model."""
        bounds = getattr(self, "_preview_bounds", None)
        if bounds is None or not self.viewport.available:
            return
        try:
            from gui.flow_overlay import build_overlay, describe

            items = build_overlay(bounds, self.aoa.value(), self.sideslip.value())
        except Exception as error:  # noqa: BLE001 - a picture, not a result
            self.append_log(f"Could not draw the flow arrows: {error}")
            return
        self.viewport.set_overlay(items)
        tilt = self.tilt_axis.currentData() or self._auto_pitch_axis()
        self.overlay_note.setText(
            describe(self.aoa.value(), self.sideslip.value(), tilt)
        )

    def _check_nose_button(self, nose_points_to: str) -> None:
        """Light the button for the direction the nose points in the CAD."""
        for button in self.axis_buttons.buttons():
            button.setChecked(button.text() == nose_points_to)

    # -- geometry preview --------------------------------------------------

    def _start_preview(self) -> None:
        """Tessellate the loaded CAD and show it standing up, from the side.

        The picture is a check, not decoration: the model goes through the
        same alignment the mesh will, and is then stood on its tail with the
        nose along +Z. A rocket that appears upside down has its nose axis
        set the wrong way round, and that is visible before anyone pays for
        a mesh.
        """
        if not self.viewport.available:
            return
        path = Path(self.step_path.text().strip())
        if not path.is_file():
            return
        try:
            if path.stat().st_size > MAX_PREVIEW_BYTES:
                self.append_log(
                    f"Preview skipped: {path.name} is larger than "
                    f"{MAX_PREVIEW_BYTES // (1024 * 1024)} MB."
                )
                return
        except OSError:  # pragma: no cover - vanished between checks
            return

        try:
            geometry = self.geometry_params()
        except Exception as error:  # noqa: BLE001 - a picture is not worth failing over
            self._preview_failed(str(error))
            return

        self._preview_token += 1
        worker = GeometryPreviewWorker(geometry, self._preview_token)
        worker.signals.finished.connect(self._preview_ready)
        worker.signals.failed.connect(self._preview_failed)
        self.pool.start(worker)

    def _preview_ready(self, payload: Any) -> None:
        """Show a finished preview, unless it has been overtaken."""
        token, preview = payload
        if token != self._preview_token:
            # The operator opened another file while this one was rendering.
            return
        try:
            import numpy as np
            import pyvista as pv

            from core.frames import points_to_rocket

            faces = np.hstack(
                [
                    np.full((len(preview.triangles), 1), 3, dtype=np.int64),
                    preview.triangles,
                ]
            ).ravel()
            # Tessellated in the solver frame, shown in the rocket frame:
            # nose up along +Z, however the CAD was drawn.
            surface = pv.PolyData(points_to_rocket(preview.points), faces)
        except Exception as error:  # noqa: BLE001 - preview is optional
            self._preview_failed(str(error))
            return

        self.viewport.show_mesh(
            surface, color="#8aa0c0", show_edges=False, smooth_shading=True
        )
        self._preview_bounds = tuple(surface.bounds)
        self._refresh_overlay()
        # Set the camera directly: the combo may already read "side", and
        # then setting it emits nothing and leaves the camera where
        # show_mesh's reset_camera put it.
        self.viewport._apply_view("side")
        # Keep the combo honest about which view is on screen, without
        # letting it re-drive the camera it has just been told about.
        was_blocked = self.viewport.view.blockSignals(True)
        self.viewport.view.setCurrentText("side")
        self.viewport.view.blockSignals(was_blocked)
        self.append_log(
            f"Preview: {len(preview.triangles)} triangles, "
            f"{preview.metrics.reference_length_m:.3f} m long, shown nose-up "
            "along +Z."
        )

    def _preview_failed(self, message: str) -> None:
        """Note a preview that could not be built, and carry on.

        A body that will not tessellate coarsely may still mesh properly
        with the real settings, so this is never allowed to block an import
        or look like a verdict on the geometry.
        """
        self.append_log(f"Preview unavailable: {message.splitlines()[0]}")

    def _on_velocity_type(self) -> None:
        """Rescale the speed slider when the unit changes."""
        velocity_type = VelocityType(self.velocity_type.currentData())
        self._rescale_velocity_slider(velocity_type)
        self.velocity.setValue(2.0 if velocity_type is VelocityType.MACH else 200.0)
        self._update_condition_label()

    def _rescale_velocity_slider(self, velocity_type: VelocityType) -> None:
        """Set the speed slider's range for the selected unit.

        Kept separate from the change handler so loading a project can adjust
        the range without also resetting the value the project specified.
        """
        if velocity_type is VelocityType.MACH:
            self.velocity.slider.setRange(20, 350)
        else:
            self.velocity.slider.setRange(1000, 120000)

    def _update_condition_label(self, *_: Any) -> None:
        """Show the derived freestream state beneath the sliders."""
        try:
            flow = self.flow_params()
            state = flow.atmosphere()
            self.condition_label.setText(
                f"M {flow.mach():.3f}  |  V {flow.speed_ms():.1f} m/s  |  "
                f"p {state.pressure_pa / 1000.0:.1f} kPa  |  "
                f"T {state.temperature_k:.1f} K"
            )
        except Exception as error:
            self.condition_label.setText(str(error))

    def _update_orientation_preview(self, *_: Any) -> None:
        """Reflect attitude changes in the status line and the 3D arrows.

        The mesh is built in the body frame and the solver applies alpha and
        beta itself, so this is presentation only.
        """
        self.statusMessage.emit(
            f"Attitude: alpha {self.aoa.value():.1f} deg, "
            f"beta {self.sideslip.value():.1f} deg"
        )
        self._refresh_overlay()

    def _draw_hinge(self, *_: Any) -> None:
        """Draw the hinge axis into the 3D scene."""
        if not self.viewport.available:
            return
        import numpy as np

        point = np.array(self.hinge_point.value())
        direction = np.array(self.hinge_direction.value())
        norm = np.linalg.norm(direction)
        if norm < 1e-9:
            self.append_log("Hinge direction cannot be zero.")
            return
        direction = direction / norm
        length = 0.15
        self.viewport.add_line(point - direction * length, point + direction * length)

    # -- parameter assembly ------------------------------------------------

    def geometry_params(self) -> GeometryParams:
        """Build GeometryParams from the form."""
        button = self.axis_buttons.checkedButton()
        # The buttons say where the nose points; meshing wants nose-to-tail.
        direction = (
            AxisDirection(_opposite_axis(button.text()))
            if button
            else AxisDirection.PLUS_X
        )
        custom = self.custom_nose.isChecked()
        tilt = self.tilt_axis.currentData()
        pitch = self._auto_pitch_axis() if tilt is None else tilt
        return GeometryParams(
            step_file_path=self.step_path.text().strip(),
            nose_direction=None if custom else direction,
            nose_vector=[-v for v in self.nose_vector.value()] if custom else None,
            body_kind=BodyKind(self.body_kind.currentData() or "rocket"),
            pitch_axis=AxisDirection(pitch) if pitch else None,
            reference_origin=self.reference_origin.value(),
            scale_to_meters=self.scale.value(),
        )

    def solver_params(self) -> SolverParams:
        """Build SolverParams from the form; Auto fields are left to the regime."""
        scheme = self.scheme.currentData()
        return SolverParams(
            mpi_ranks=self.ranks.value(),
            convective_scheme=ConvectiveScheme(scheme) if scheme else None,
            cfl_number=_auto_value(self.cfl_start),
            cfl_growth=_auto_value(self.cfl_growth),
            cfl_max=_auto_value(self.cfl_max),
            max_iterations=self.max_iterations.value(),
        )

    def domain_params(self) -> DomainParams:
        """Build DomainParams from the form."""
        return DomainParams(
            shape=DomainShape(self.domain_shape.currentData()),
            upstream_multiplier=self.upstream.value(),
            downstream_multiplier=self.downstream.value(),
            radial_multiplier=self.radial.value(),
        )

    def flow_params(self) -> FlowParams:
        """Build FlowParams from the form."""
        return FlowParams(
            velocity_type=VelocityType(self.velocity_type.currentData()),
            velocity_value=self.velocity.value(),
            aoa_deg=self.aoa.value(),
            sideslip_deg=self.sideslip.value(),
            altitude_m=self.altitude.value(),
        )

    def hinge_axes(self) -> list[HingeAxis]:
        """The configured hinge axis, if its direction is valid."""
        direction = self.hinge_direction.value()
        if sum(component**2 for component in direction) < 1e-18:
            return []
        return [
            HingeAxis(
                name=self.hinge_name.text().strip() or "fin_1",
                point=self.hinge_point.value(),
                direction=direction,
                frame=Frame.ROCKET,
            )
        ]

    # -- project round trip ------------------------------------------------

    def write_to_project(self, project: Project) -> None:
        """Copy the tab's current state into a project.

        Geometry is only recorded once a CAD file has been chosen, so saving
        an untouched project does not bake in an empty path that would later
        fail validation.
        """
        if self.step_path.text().strip():
            project.geometry = self.geometry_params()
        project.domain = self.domain_params()
        project.flow = self.flow_params()
        project.hinge_axes = self.hinge_axes()
        project.mesh = MeshParams(
            resolution=MeshResolution(self.resolution.currentData()),
            track=SimulationTrack.AERODYNAMIC,
            boundary_layers=self.layers.value(),
            target_yplus=self.target_yplus.value(),
        )
        project.solver = self.solver_params()
        project.sweep = SweepSettings.from_text(
            self.sweep_parameter.currentText(), self.sweep_values.text()
        )
        project.last_mesh_id = self.mesh_id

    def apply_project(self, project: Project) -> None:
        """Populate the tab from a project.

        Signals are blocked during the update so the half-applied state does
        not trigger recomputation, and restored before the final refresh.
        """
        widgets = [
            self.step_path, self.custom_nose, self.nose_vector,
            self.reference_origin, self.scale, self.domain_shape,
            self.upstream, self.downstream, self.radial,
            self.velocity_type, self.velocity, self.aoa, self.sideslip,
            self.altitude, self.hinge_name, self.hinge_point,
            self.hinge_direction, self.resolution, self.layers,
            self.target_yplus, self.ranks, self.scheme, self.cfl_start,
            self.body_kind, self.tilt_axis,
            self.cfl_growth, self.cfl_max, self.max_iterations, self.sweep_values,
            self.sweep_parameter,
        ]
        for widget in widgets:
            widget.blockSignals(True)
        self._restoring = True
        try:
            self._apply_project_locked(project)
        finally:
            self._restoring = False
            for widget in widgets:
                widget.blockSignals(False)

        self._update_condition_label()
        self.mesh_id = project.last_mesh_id
        has_mesh = self._mesh_is_usable(project.last_mesh_id)
        self.run_button.setEnabled(has_mesh)
        self.sweep_button.setEnabled(has_mesh)
        if project.last_mesh_id and not has_mesh:
            self.mesh_id = None
            self.append_log(
                f"Mesh {project.last_mesh_id} referenced by the project is no "
                "longer available; generate a new one."
            )

    def _apply_project_locked(self, project: Project) -> None:
        """Write project values into the widgets, signals already blocked."""
        geometry = project.geometry
        if geometry is not None:
            self.step_path.setText(geometry.step_file_path)
            self.scale.setValue(geometry.scale_to_meters)
            self.reference_origin.set_value(tuple(geometry.reference_origin))
            self.custom_nose.setChecked(geometry.nose_vector is not None)
            self.nose_vector.setEnabled(geometry.nose_vector is not None)
            if geometry.nose_vector is not None:
                self.nose_vector.set_value(tuple(-v for v in geometry.nose_vector))
            elif geometry.nose_direction is not None:
                self._check_nose_button(_opposite_axis(geometry.nose_direction.value))
            self.body_kind.setCurrentIndex(
                max(0, self.body_kind.findData(geometry.body_kind.value))
            )
            self.tilt_axis.setCurrentIndex(
                max(0, self.tilt_axis.findData(
                    geometry.pitch_axis.value if geometry.pitch_axis else None
                ))
            )
            # After the scale and the nose axis, so the project's own values
            # are what reach the assistant.
            self._on_step_path_changed(keep_scale=True)

        domain = project.domain
        index = self.domain_shape.findText(domain.shape.value)
        if index >= 0:
            self.domain_shape.setCurrentIndex(index)
        self.upstream.setValue(domain.upstream_multiplier)
        self.downstream.setValue(domain.downstream_multiplier)
        self.radial.setValue(domain.radial_multiplier)

        flow = project.flow
        type_index = self.velocity_type.findData(flow.velocity_type.value)
        if type_index < 0:
            type_index = 0 if flow.velocity_type is VelocityType.MACH else 1
        self.velocity_type.setCurrentIndex(type_index)
        self._rescale_velocity_slider(flow.velocity_type)
        self.velocity.setValue(flow.velocity_value)
        self.aoa.setValue(flow.aoa_deg)
        self.sideslip.setValue(flow.sideslip_deg)
        if flow.altitude_m is not None:
            self.altitude.setValue(flow.altitude_m)

        if project.hinge_axes:
            # Older projects hold their hinges in the solver frame.
            hinge = project.hinge_axes[0].in_frame(Frame.ROCKET)
            self.hinge_name.setText(hinge.name)
            self.hinge_point.set_value(tuple(hinge.point))
            self.hinge_direction.set_value(tuple(hinge.direction))

        mesh_index = self.resolution.findText(project.mesh.resolution.value)
        if mesh_index >= 0:
            self.resolution.setCurrentIndex(mesh_index)
        self.layers.setValue(project.mesh.boundary_layers)
        self.target_yplus.setValue(project.mesh.target_yplus)
        self.ranks.setValue(project.solver.mpi_ranks)
        solver = project.solver
        self.scheme.setCurrentIndex(
            max(
                0,
                self.scheme.findData(
                    solver.convective_scheme.value
                    if solver.convective_scheme is not None
                    else None
                ),
            )
        )
        _set_auto(self.cfl_start, solver.cfl_number)
        _set_auto(self.cfl_growth, solver.cfl_growth)
        _set_auto(self.cfl_max, solver.cfl_max)
        self.max_iterations.setValue(solver.max_iterations)

        sweep_index = self.sweep_parameter.findText(project.sweep.parameter)
        if sweep_index >= 0:
            self.sweep_parameter.setCurrentIndex(sweep_index)
        self.sweep_values.setText(project.sweep.values_text())

    def _mesh_is_usable(self, mesh_id: str | None) -> bool:
        """True when a stored mesh still exists on disk."""
        if not mesh_id:
            return False
        try:
            self.store.resolve_mesh_path(mesh_id)
        except Exception:
            return False
        return True

    # -- actions -----------------------------------------------------------

    def generate_mesh(self) -> None:
        """Start meshing on a worker thread."""
        if not self.step_path.text().strip():
            self._warn("Select a STEP file first.")
            return
        try:
            request = MeshRequest(
                geometry=self.geometry_params(),
                domain=self.domain_params(),
                mesh=MeshParams(
                    resolution=MeshResolution(self.resolution.currentData()),
                    track=SimulationTrack.AERODYNAMIC,
                    boundary_layers=self.layers.value(),
                    target_yplus=self.target_yplus.value(),
                ),
                sizing_flow=self.flow_params(),
            )
        except Exception as error:
            self._warn(f"Invalid settings:\n{error}")
            return

        record = self.store.create("mesh", {"step_file_path": request.geometry.step_file_path})
        self.mesh_id = record.record_id
        worker = MeshWorker(request, record.path("mesh.su2"))
        worker.signals.progress.connect(self.append_log)
        worker.signals.finished.connect(self._mesh_finished)
        worker.signals.failed.connect(self._operation_failed)
        self._start(worker, "Meshing ...")

    def _mesh_finished(self, result: Any) -> None:
        """Record the mesh and enable the solver buttons."""
        self._finish()
        self.mesh_result = result
        self.store.update_metadata(
            self.mesh_id,
            {
                "mesh_path": result.mesh_path,
                "cell_count": result.cell_count,
                "reference_area_m2": result.reference_area_m2,
                "reference_length_m": result.reference_length_m,
            },
        )
        self.append_log(
            f"Mesh ready: {result.cell_count:,} cells, y+ "
            f"{result.estimated_yplus:.1f}, quality {result.min_quality:.3f}"
        )
        if not result.within_target_band:
            self.append_log(
                f"  note: outside the target band {result.target_band}"
            )
        self.run_button.setEnabled(True)
        self.sweep_button.setEnabled(True)
        self.statusMessage.emit(f"Mesh {self.mesh_id} ready")

    def run_simulation(self) -> None:
        """Start a single aerodynamic solve."""
        if not self.mesh_id:
            self._warn("Generate a mesh first.")
            return
        try:
            request = AeroRunRequest(
                mesh_id=self.mesh_id,
                flow=self.flow_params(),
                solver=self.solver_params(),
                hinge_axes=self.hinge_axes(),
            )
        except Exception as error:
            self._warn(f"Invalid settings:\n{error}")
            return

        from backend.runner import SU2Runner

        record = self.store.create("aero", {"mesh_id": self.mesh_id})
        mesh_record = self.store.get(self.mesh_id)
        self.chart.clear()
        self.chart.setVisible(True)

        worker = AeroWorker(
            request,
            mesh_path=Path(mesh_record.metadata["mesh_path"]),
            working_directory=record.directory,
            runner=SU2Runner(),
            reference_area_m2=float(mesh_record.metadata.get("reference_area_m2", 1.0)),
            reference_length_m=float(mesh_record.metadata.get("reference_length_m", 1.0)),
        )
        worker.signals.progress.connect(self.append_log)
        worker.signals.progress.connect(self._watch_for_stage_change)
        worker.signals.iteration.connect(self.chart.add_record)
        worker.signals.finished.connect(self._run_finished)
        worker.signals.failed.connect(self._operation_failed)
        self._start(worker, "Solving ...")

    def _watch_for_stage_change(self, line: str) -> None:
        """Keep the convergence chart continuous across a rescue stage."""
        from backend.aero_solver import STAGE_MARKER_PREFIX

        if line.startswith(STAGE_MARKER_PREFIX):
            self.chart.begin_stage()

    def _run_finished(self, result: Any) -> None:
        """Populate the result cards and record the run."""
        self._finish()
        self.show_result(result)

        # Recorded the way the MCP tool records it, so the assistant, the
        # Graphics tab and a later session all see the same run.
        try:
            self.store.write_json(result.sim_id, "result.json", result)
            self.store.update_metadata(
                result.sim_id,
                {
                    "cd": result.cd,
                    "cl": result.cl,
                    "mach": result.mach,
                    "aoa_deg": result.aoa_deg,
                },
            )
        except Exception as error:  # noqa: BLE001 - the result is on screen
            self.append_log(f"Could not record the result: {error}")
        self.simulationFinished.emit(result.sim_id)

    def open_run(self, record_id: str) -> bool:
        """Load a stored mesh or simulation back into the tab.

        A simulation brings its result cards back and makes its mesh the
        current one, so the next run or sweep continues on it; a mesh just
        becomes the current mesh.
        """
        from core.models import AeroResult

        try:
            record = self.store.get(record_id)
        except Exception:
            self.append_log(f"{record_id} is no longer in the run registry.")
            return False
        mesh_id = record_id if record.kind == "mesh" else record.metadata.get("mesh_id")
        if mesh_id and self._mesh_is_usable(mesh_id):
            self.mesh_id = mesh_id
            self.run_button.setEnabled(True)
            self.sweep_button.setEnabled(True)
            self.append_log(f"Current mesh: {mesh_id}")
        if record.kind == "aero" and record.exists("result.json"):
            try:
                result = AeroResult.model_validate(self.store.read_json(record_id, "result.json"))
            except Exception as error:  # noqa: BLE001 - reported
                self.append_log(f"Could not read the result of {record_id}: {error}")
                return False
            self.show_result(result)
            self.append_log(f"Loaded simulation {record_id}")
        self.statusMessage.emit(f"Opened {record_id}")
        return True

    def show_result(self, result: Any) -> None:
        """Fill the result cards and log from a finished simulation."""
        self.card_cd.set_value(result.cd)
        self.card_cl.set_value(result.cl)
        self.card_drag.set_value(result.drag_n, "{:.2f}")
        self.card_lift.set_value(result.lift_n, "{:.2f}")
        if result.hinge_torques:
            self.card_torque.set_value(result.hinge_torques[0].torque_nm, "{:.4f}")
        self.card_cop.set_value(result.center_of_pressure_rocket[2], "{:.4f}")
        for card, value in zip(
            (self.card_fx, self.card_fy, self.card_fz), result.force_rocket_n
        ):
            card.set_value(value, "{:.2f}")
        # A rescued run converged in the end, but it needed help getting
        # there -- which the operator should see on the card, not only in the
        # log they may have scrolled past.
        self.card_cd.set_colour(
            SUCCESS if result.converged and not result.rescued
            else WARNING if result.converged
            else DANGER
        )
        self.append_log(
            f"Converged={result.converged} after {result.iterations} iterations "
            f"({result.wall_time_s:.1f}s)"
        )
        fx, fy, fz = result.force_rocket_n
        self.append_log(
            f"Force in rocket axes: X {fx:.2f} N, Y {fy:.2f} N, Z {fz:.2f} N "
            "(Z along the nose)"
        )
        for note in getattr(result, "notes", []):
            self.append_log(note)

    def run_sweep(self) -> None:
        """Start a parametric sweep."""
        if not self.mesh_id:
            self._warn("Generate a mesh first.")
            return
        try:
            values = [
                float(token)
                for token in self.sweep_values.text().replace(";", ",").split(",")
                if token.strip()
            ]
        except ValueError:
            self._warn("Sweep values must be a comma-separated list of numbers.")
            return
        if not values:
            self._warn("Enter at least one sweep value.")
            return

        from backend.runner import SU2Runner

        record = self.store.create("sweep", {"mesh_id": self.mesh_id})
        mesh_record = self.store.get(self.mesh_id)
        worker = SweepWorker(
            AeroRunRequest(
                mesh_id=self.mesh_id,
                flow=self.flow_params(),
                solver=self.solver_params(),
                hinge_axes=self.hinge_axes(),
            ),
            parameter=self.sweep_parameter.currentText(),
            values=values,
            mesh_path=Path(mesh_record.metadata["mesh_path"]),
            output_root=record.directory,
            runner=SU2Runner(),
            reference_area_m2=float(mesh_record.metadata.get("reference_area_m2", 1.0)),
            reference_length_m=float(mesh_record.metadata.get("reference_length_m", 1.0)),
        )
        worker.signals.progress.connect(self.append_log)
        worker.signals.finished.connect(self._sweep_finished)
        worker.signals.failed.connect(self._operation_failed)
        self._start(worker, f"Sweeping {len(values)} points ...")

    def _sweep_finished(self, sweep: Any) -> None:
        """Summarise a completed sweep."""
        self._finish()
        extremes = sweep.extremes()
        self.append_log(
            f"Sweep complete: {len(sweep.succeeded_points)} of "
            f"{len(sweep.points)} points converged"
        )
        if "servo_sizing_torque_nm" in extremes:
            self.card_torque.set_value(extremes["servo_sizing_torque_nm"], "{:.4f}")
            self.append_log(
                f"  peak hinge torque {extremes['servo_sizing_torque_nm']:.4f} N m"
            )

    def cancel(self) -> None:
        """Cancel the running operation."""
        if self._worker is not None:
            self._worker.cancel()
            self.append_log("Cancellation requested ...")

    # -- worker plumbing ---------------------------------------------------

    def _start(self, worker: Any, message: str) -> None:
        """Run a worker and put the UI into its busy state."""
        self._worker = worker
        self.progress.setVisible(True)
        self.cancel_button.setEnabled(True)
        for button in (self.mesh_button, self.run_button, self.sweep_button):
            button.setEnabled(False)
        self.append_log(message)
        self.statusMessage.emit(message)
        self._step_text = message.rstrip(" .…")
        self._step_started = time.monotonic()
        self._operation_started = self._step_started
        self.step_label.setVisible(True)
        self._refresh_step_clock()
        self._step_timer.start(STEP_CLOCK_INTERVAL_MS)
        self.pool.start(worker)

    def _finish(self) -> None:
        """Restore the UI after a worker completes."""
        self._worker = None
        self._stop_step_clock()
        self.progress.setVisible(False)
        self.chart.setVisible(False)
        self.cancel_button.setEnabled(False)
        self.mesh_button.setEnabled(True)
        self.run_button.setEnabled(self.mesh_id is not None)
        self.sweep_button.setEnabled(self.mesh_id is not None)

    def _operation_failed(self, message: str) -> None:
        """Report a worker failure."""
        self._finish()
        self.append_log(f"FAILED: {message}")
        self.statusMessage.emit("Operation failed")

    def append_log(self, message: str) -> None:
        """Append a line to the log panel and restart the step clock.

        Each incoming line marks the start of whatever comes next, so the
        clock beside the progress bar measures the step actually running
        rather than the whole operation.
        """
        self.log.appendPlainText(message)
        stripped = message.strip()
        # A completion line ("… — 12.3 s") closes a step rather than opening
        # one; leaving the clock on it would count time nothing is spending.
        if stripped.endswith("…"):
            self._step_text = stripped.rstrip(" …")
            self._step_started = time.monotonic()
            self._refresh_step_clock()

    def _refresh_step_clock(self) -> None:
        """Redraw the current step with its elapsed time."""
        if not self._step_text or self._step_started is None:
            self.step_label.clear()
            return
        now = time.monotonic()
        step = format_duration(now - self._step_started)
        total = format_duration(now - (self._operation_started or self._step_started))
        self.step_label.setText(f"{self._step_text} — step {step} · total {total}")

    def _stop_step_clock(self) -> None:
        """Forget the current step once the worker has finished."""
        self._step_timer.stop()
        self._step_text = ""
        self._step_started = None
        self.step_label.clear()
        self.step_label.setVisible(False)

    def _warn(self, message: str) -> None:
        """Show a modal warning."""
        QtWidgets.QMessageBox.warning(self, APP_NAME, message)


# ---------------------------------------------------------------------------
# Tab 2: sensor microclimate
# ---------------------------------------------------------------------------


class SensorTab(QtWidgets.QWidget):
    """BMP580 microclimate: enclosure, environment and the predicted bias."""

    statusMessage = QtCore.Signal(str)

    def __init__(
        self, store: RunStore, parent: QtWidgets.QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.pool = QtCore.QThreadPool.globalInstance()

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_controls())
        splitter.addWidget(self._build_display())
        splitter.setSizes([420, 900])

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(splitter)

        self.recompute()

    def _build_controls(self) -> QtWidgets.QWidget:
        """Environment and enclosure inputs."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setSpacing(10)

        geometry = QtWidgets.QGroupBox("Enclosure")
        form = QtWidgets.QFormLayout(geometry)
        self.step_path = QtWidgets.QLineEdit()
        self.step_path.setPlaceholderText("Enclosure .step file (optional)")
        browse = QtWidgets.QPushButton("Browse")
        browse.clicked.connect(self._browse)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.step_path, 1)
        row.addWidget(browse)
        form.addRow("STEP file", _wrap(row))

        self.sensor_xyz = VectorInput((0.030, 0.020, 0.015))
        self.sensor_xyz.setToolTip("BMP580 die coordinate inside the enclosure")
        form.addRow("Sensor position", self.sensor_xyz)
        layout.addWidget(geometry)

        environment = QtWidgets.QGroupBox("Environment")
        form = QtWidgets.QFormLayout(environment)

        self.speed = LabelledSlider(0.0, 20.0, 2.0, decimals=1, suffix=" m/s")
        self.speed.setToolTip("Tram speed driving ram air through the intake")
        form.addRow("Vehicle speed", self.speed)

        self.ambient = LabelledSlider(-20.0, 50.0, 25.0, decimals=1, suffix=" C")
        form.addRow("Ambient air", self.ambient)

        self.solar = LabelledSlider(0.0, 1200.0, 800.0, decimals=0, suffix=" W/m2")
        form.addRow("Solar flux", self.solar)

        self.height = LabelledSlider(0.01, 1.0, 0.10, decimals=3, suffix=" m")
        self.height.setToolTip(
            "Intake height above the roof; compared against the roof's thermal "
            "boundary layer to decide whether ambient air is sampled"
        )
        form.addRow("Height above roof", self.height)
        layout.addWidget(environment)

        surfaces = QtWidgets.QGroupBox("Surface properties")
        form = QtWidgets.QFormLayout(surfaces)
        self.housing_absorptivity = LabelledSlider(0.0, 1.0, 0.30, decimals=2)
        self.housing_absorptivity.setToolTip(
            "Solar absorptivity of the housing; a light finish is the cheapest "
            "way to reduce the error"
        )
        form.addRow("Housing absorptivity", self.housing_absorptivity)
        self.roof_absorptivity = LabelledSlider(0.0, 1.0, 0.65, decimals=2)
        form.addRow("Roof absorptivity", self.roof_absorptivity)
        self.conductivity = QtWidgets.QDoubleSpinBox()
        self.conductivity.setDecimals(3)
        self.conductivity.setRange(0.01, 500.0)
        self.conductivity.setValue(0.18)
        self.conductivity.setSuffix(" W/(m K)")
        form.addRow("Housing conductivity", self.conductivity)
        layout.addWidget(surfaces)

        for slider in (
            self.speed, self.ambient, self.solar, self.height,
            self.housing_absorptivity, self.roof_absorptivity,
        ):
            slider.valueChanged.connect(lambda *_: self.recompute())
        self.conductivity.valueChanged.connect(lambda *_: self.recompute())

        layout.addStretch(1)
        return panel

    def _build_display(self) -> QtWidgets.QWidget:
        """Readouts and the temperature scene."""
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setSpacing(8)

        heading = QtWidgets.QLabel("PREDICTED BMP580 READING")
        heading.setObjectName("heading")
        layout.addWidget(heading)

        cards = QtWidgets.QHBoxLayout()
        self.card_sensor = ResultCard("Sensor reads", "C")
        self.card_ambient = ResultCard("True ambient", "C")
        self.card_bias = ResultCard("Measurement error", "K")
        for card in (self.card_sensor, self.card_ambient, self.card_bias):
            cards.addWidget(card)
        layout.addLayout(cards)

        detail = QtWidgets.QHBoxLayout()
        self.card_roof = ResultCard("Roof surface", "C")
        self.card_housing = ResultCard("Housing", "C")
        self.card_flow = ResultCard("Intake flow", "mg/s")
        self.card_layer = ResultCard("Roof thermal layer", "mm")
        for card in (
            self.card_roof, self.card_housing, self.card_flow, self.card_layer
        ):
            detail.addWidget(card)
        layout.addLayout(detail)

        self.verdict = QtWidgets.QLabel()
        self.verdict.setWordWrap(True)
        self.verdict.setObjectName("hint")
        layout.addWidget(self.verdict)

        self.viewport = Viewport()
        layout.addWidget(self.viewport, 1)
        return panel

    def _browse(self) -> None:
        """Pick the enclosure CAD file."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select the enclosure STEP file", "", "STEP files (*.step *.stp)"
        )
        if path:
            self.step_path.setText(path)

    def thermal_params(self) -> ThermalParams:
        """Build ThermalParams from the form."""
        return ThermalParams(
            enclosure_step_path=self.step_path.text().strip() or "(not supplied)",
            vehicle_speed_ms=self.speed.value(),
            ambient_temp_c=self.ambient.value(),
            solar_flux_w_m2=self.solar.value(),
            height_above_roof_m=self.height.value(),
            sensor_xyz=self.sensor_xyz.value(),
            housing_conductivity_w_mk=self.conductivity.value(),
            housing_solar_absorptivity=self.housing_absorptivity.value(),
            roof_solar_absorptivity=self.roof_absorptivity.value(),
        )

    def write_to_project(self, project: Project) -> None:
        """Copy the sensor scenario into a project."""
        project.thermal = self.thermal_params()

    def apply_project(self, project: Project) -> None:
        """Populate the sensor tab from a project."""
        thermal = project.thermal
        if thermal is None:
            return

        widgets = [
            self.step_path, self.sensor_xyz, self.speed, self.ambient,
            self.solar, self.height, self.housing_absorptivity,
            self.roof_absorptivity, self.conductivity,
        ]
        for widget in widgets:
            widget.blockSignals(True)
        try:
            if thermal.enclosure_step_path not in ("", "(not supplied)"):
                self.step_path.setText(thermal.enclosure_step_path)
            self.sensor_xyz.set_value(tuple(thermal.sensor_xyz))
            self.speed.setValue(thermal.vehicle_speed_ms)
            self.ambient.setValue(thermal.ambient_temp_c)
            self.solar.setValue(thermal.solar_flux_w_m2)
            self.height.setValue(thermal.height_above_roof_m)
            self.housing_absorptivity.setValue(thermal.housing_solar_absorptivity)
            self.roof_absorptivity.setValue(thermal.roof_solar_absorptivity)
            self.conductivity.setValue(thermal.housing_conductivity_w_mk)
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self.recompute()

    def recompute(self) -> None:
        """Update the readouts from the analytical model.

        Fast enough to run on the GUI thread as sliders move, which is what
        makes the tab feel like a design tool rather than a batch job.
        """
        try:
            from backend.thermal_solver import estimate_sensor_bias

            result = estimate_sensor_bias(self.thermal_params())
        except Exception as error:
            self.verdict.setText(f"Invalid settings: {error}")
            return

        self.card_sensor.set_value(result.sensor_temp_k - 273.15, "{:.2f}")
        self.card_ambient.set_value(result.ambient_temp_k - 273.15, "{:.2f}")
        self.card_bias.set_value(result.delta_t_error_k, "{:+.2f}")
        self.card_roof.set_value(result.roof_temp_k - 273.15, "{:.1f}")
        self.card_housing.set_value(result.housing_temp_k - 273.15, "{:.1f}")
        self.card_flow.set_value(result.tube_mass_flow_kg_s * 1e6, "{:.1f}")
        self.card_layer.set_value(result.thermal_boundary_layer_m * 1000.0, "{:.1f}")

        magnitude = abs(result.delta_t_error_k)
        colour = SUCCESS if magnitude < 0.5 else (WARNING if magnitude < 2.0 else DANGER)
        self.card_bias.set_colour(colour)

        if result.intake_in_roof_plume:
            verdict = (
                "The intake sits inside the roof's thermal boundary layer, so it "
                "is sampling air already warmed by the roof. Raising the intake "
                "above the layer thickness shown would reduce the error markedly."
            )
        else:
            verdict = (
                "The intake is clear of the roof's thermal boundary layer and is "
                "sampling ambient air. The remaining error comes from the "
                "sun-warmed housing walls heating the sampled air in the chamber."
            )
        self.verdict.setText(verdict)
        self.statusMessage.emit(
            f"Predicted bias {result.delta_t_error_k:+.2f} K"
        )


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------


class ProjectDetailsDialog(QtWidgets.QDialog):
    """Edit a project's name, description, author and working notes."""

    def __init__(self, project: Project, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Project details")
        self.setMinimumWidth(520)

        self.name = QtWidgets.QLineEdit(project.metadata.name)
        self.description = QtWidgets.QLineEdit(project.metadata.description)
        self.author = QtWidgets.QLineEdit(project.metadata.author)
        self.notes = QtWidgets.QPlainTextEdit(project.notes)
        self.notes.setPlaceholderText(
            "Working notes carried with the project: what you are testing, "
            "what you have already ruled out, anything a colleague opening "
            "this file would need to know."
        )

        form = QtWidgets.QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Description", self.description)
        form.addRow("Author", self.author)
        form.addRow("Notes", self.notes)

        created = QtWidgets.QLabel(
            f"Created {project.metadata.created_at}  |  "
            f"last saved {project.metadata.modified_at}"
        )
        created.setObjectName("hint")

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(created)
        layout.addWidget(buttons)

    def apply_to(self, project: Project) -> None:
        """Write the edited values back into the project."""
        project.metadata.name = self.name.text().strip() or "Untitled project"
        project.metadata.description = self.description.text().strip()
        project.metadata.author = self.author.text().strip()
        project.notes = self.notes.toPlainText()
        project.touch()


class SettingsDialog(QtWidgets.QDialog):
    """Edit the persistent application preferences."""

    def __init__(
        self, settings: AppSettings, parent: QtWidgets.QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(480)
        self._settings = settings

        self.ranks = QtWidgets.QSpinBox()
        self.ranks.setRange(1, 64)
        self.ranks.setValue(settings.default_mpi_ranks)
        self.ranks.setToolTip(
            "MPI ranks a new run starts with. Leave two below the thread count "
            "so the interface stays responsive."
        )

        self.colormap = QtWidgets.QComboBox()
        self.colormap.addItems(
            ["turbo", "coolwarm", "viridis", "jet", "plasma", "inferno"]
        )
        self.colormap.setCurrentText(settings.default_colormap)

        self.resolution = QtWidgets.QComboBox()
        self.resolution.addItems(["preview", "hd", "2k", "4k"])
        self.resolution.setCurrentText(settings.default_render_resolution)

        self.mesh_resolution = QtWidgets.QComboBox()
        self.mesh_resolution.addItems(["coarse", "medium", "fine"])
        self.mesh_resolution.setCurrentText(settings.default_mesh_resolution)

        self.confirm_on_exit = QtWidgets.QCheckBox(
            "Ask before discarding an unsaved project"
        )
        self.confirm_on_exit.setChecked(settings.confirm_on_exit)

        self.autosave = QtWidgets.QCheckBox(
            "Save the open project automatically before a solve"
        )
        self.autosave.setChecked(settings.autosave_projects)

        self.live_preview = QtWidgets.QCheckBox(
            "Update the sensor prediction live as sliders move"
        )
        self.live_preview.setChecked(settings.live_thermal_preview)

        self.environment_warning = QtWidgets.QCheckBox(
            "Warn at startup when SU2 or MPI cannot be found"
        )
        self.environment_warning.setChecked(settings.show_environment_warning)

        form = QtWidgets.QFormLayout()
        form.addRow("Default MPI ranks", self.ranks)
        form.addRow("Default colormap", self.colormap)
        form.addRow("Render resolution", self.resolution)
        form.addRow("Mesh resolution", self.mesh_resolution)

        behaviour = QtWidgets.QVBoxLayout()
        for check in (
            self.confirm_on_exit, self.autosave,
            self.live_preview, self.environment_warning,
        ):
            behaviour.addWidget(check)

        location = QtWidgets.QLabel(f"Settings file: {AppSettings.path()}")
        location.setObjectName("hint")
        location.setWordWrap(True)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(_separator())
        layout.addLayout(behaviour)
        layout.addWidget(location)
        layout.addWidget(buttons)

    def updated_settings(self) -> AppSettings:
        """A copy of the settings with the dialog's values applied."""
        updated = self._settings.model_copy(deep=True)
        updated.default_mpi_ranks = self.ranks.value()
        updated.default_colormap = self.colormap.currentText()
        updated.default_render_resolution = self.resolution.currentText()
        updated.default_mesh_resolution = self.mesh_resolution.currentText()
        updated.confirm_on_exit = self.confirm_on_exit.isChecked()
        updated.autosave_projects = self.autosave.isChecked()
        updated.live_thermal_preview = self.live_preview.isChecked()
        updated.show_environment_warning = self.environment_warning.isChecked()
        return updated


def _separator() -> QtWidgets.QFrame:
    """A horizontal rule between dialog sections."""
    line = QtWidgets.QFrame()
    line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
    line.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
    return line


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QtWidgets.QMainWindow):
    """Application shell holding the two tracks."""

    def __init__(
        self,
        store: RunStore | None = None,
        settings: AppSettings | None = None,
    ) -> None:
        super().__init__()
        self.store = store or default_store()
        self.settings = settings if settings is not None else load_settings()

        self.project = default_project()
        self.project_path: Path | None = None

        self.resize(self.settings.window_width, self.settings.window_height)
        if self.settings.window_maximised:
            self.showMaximized()

        self.tabs = QtWidgets.QTabWidget()
        self.aero_tab = AerodynamicsTab(self.store)
        self.sensor_tab = SensorTab(self.store)
        self.tabs.addTab(self.aero_tab, "Aerodynamics && Fins")
        # Same root the window itself saves settings to, so the assistant's
        # preferences and credentials land beside the run registry.
        self.ai_tab = AITab(self.settings, self.store.root)
        self.graphics_tab = GraphicsTab(self.store, self.settings, self.store.root)
        self.tabs.addTab(self.sensor_tab, "Sensor Microclimate (BMP580)")
        self.tabs.addTab(self.ai_tab, "AI Assistant")
        # Last, so the tab index remembered from an earlier version still
        # opens the tab it meant.
        self.tabs.addTab(self.graphics_tab, "Graphics")
        self.tabs.setCurrentIndex(
            min(self.settings.active_tab, self.tabs.count() - 1)
        )
        self.setCentralWidget(self.tabs)

        self.status = self.statusBar()
        self.aero_tab.statusMessage.connect(self.status.showMessage)
        self.sensor_tab.statusMessage.connect(self.status.showMessage)
        self.ai_tab.statusMessage.connect(self.status.showMessage)
        self.graphics_tab.statusMessage.connect(self.status.showMessage)

        # Every picture, whoever asked for it, ends up in the Graphics tab.
        self.aero_tab.simulationFinished.connect(self.graphics_tab.render_standard)
        self.ai_tab.imageProduced.connect(self.graphics_tab.select_image)
        self.ai_tab.showGraphics.connect(self.show_graphics)
        self.ai_tab.openRun.connect(self.open_run)
        self.ai_tab.openProject.connect(lambda path: self.open_project(path))

        self.aero_tab.ranks.setValue(self.settings.default_mpi_ranks)

        self._build_menu()
        self._apply_project_to_tabs()
        self._update_title()
        self._report_environment()

    def open_run(self, record_id: str) -> None:
        """Open a run a conversation refers to, where it belongs."""
        if self.aero_tab.open_run(record_id):
            self.tabs.setCurrentWidget(self.aero_tab)
            images = [
                image for image in find_rendered_images(self.store)
                if image.sim_id == record_id
            ]
            if images:
                self.graphics_tab.select_image(images[0].path)

    def show_graphics(self, path: str = "") -> None:
        """Switch to the Graphics tab, with an image selected if given."""
        if path:
            self.graphics_tab.select_image(path)
        self.tabs.setCurrentWidget(self.graphics_tab)

    # -- project management ------------------------------------------------

    def _collect_project(self) -> Project:
        """Gather the current state of both tabs into the project."""
        self.aero_tab.write_to_project(self.project)
        self.sensor_tab.write_to_project(self.project)
        return self.project

    def _apply_project_to_tabs(self) -> None:
        """Push the loaded project into both tabs."""
        self.aero_tab.apply_project(self.project)
        self.sensor_tab.apply_project(self.project)

    def _update_title(self) -> None:
        """Show the project name and file in the title bar."""
        name = self.project.metadata.name
        location = self.project_path.name if self.project_path else "unsaved"
        self.setWindowTitle(f"{name} - {location} - {APP_NAME}")

    def new_project(self) -> None:
        """Discard the current project and start a fresh one."""
        if not self._confirm_discard():
            return
        self.project = default_project()
        self.project_path = None
        self._apply_project_to_tabs()
        self._update_title()
        self.status.showMessage("New project", 4000)

    def open_project(self, path: Path | str | None = None) -> bool:
        """Open a project file, prompting for one when not given.

        Returns
        -------
        bool
            True when a project was loaded.
        """
        if path is None:
            if not self._confirm_discard():
                return False
            start = (
                self.settings.last_project_directory
                or str(projects_directory(self.store.root))
            )
            chosen, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Open project", start, PROJECT_FILTER
            )
            if not chosen:
                return False
            path = chosen

        try:
            self.project = Project.load(path)
        except ProjectError as error:
            QtWidgets.QMessageBox.warning(self, APP_NAME, str(error))
            self.settings.forget_project(path)
            self._refresh_recent_menu()
            return False

        self.project_path = Path(path)
        self._apply_project_to_tabs()
        self._update_title()
        self.settings.remember_project(self.project_path)
        save_settings(self.settings, self.store.root)
        self._refresh_recent_menu()
        self.status.showMessage(f"Opened {self.project_path.name}", 5000)
        return True

    def save_project(self) -> bool:
        """Save to the current file, or prompt when there is none."""
        if self.project_path is None:
            return self.save_project_as()
        return self._write_project(self.project_path)

    def save_project_as(self) -> bool:
        """Prompt for a destination and save there."""
        start = str(
            self.project_path
            or Path(
                self.settings.last_project_directory
                or str(projects_directory(self.store.root))
            )
            / f"{self.project.metadata.name}.atsproj"
        )
        chosen, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save project as", start, PROJECT_FILTER
        )
        if not chosen:
            return False
        return self._write_project(Path(chosen))

    def _write_project(self, path: Path) -> bool:
        """Collect the tabs and write the project to disk."""
        self._collect_project()
        try:
            written = self.project.save(path)
        except ProjectError as error:
            QtWidgets.QMessageBox.warning(self, APP_NAME, str(error))
            return False

        self.project_path = written
        self.settings.remember_project(written)
        save_settings(self.settings, self.store.root)
        self._refresh_recent_menu()
        self._update_title()
        self.status.showMessage(f"Saved {written.name}", 5000)
        return True

    def edit_project_details(self) -> None:
        """Edit the project name, description and notes."""
        dialog = ProjectDetailsDialog(self.project, self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            dialog.apply_to(self.project)
            self._update_title()

    def _confirm_discard(self) -> bool:
        """Ask before replacing the current project.

        Offers to save first, so an accidental New or Open cannot silently
        throw away a setup the operator spent time on.
        """
        if not self.settings.confirm_on_exit:
            return True
        answer = QtWidgets.QMessageBox.question(
            self,
            APP_NAME,
            "Save the current project before continuing?",
            QtWidgets.QMessageBox.StandardButton.Save
            | QtWidgets.QMessageBox.StandardButton.Discard
            | QtWidgets.QMessageBox.StandardButton.Cancel,
            QtWidgets.QMessageBox.StandardButton.Discard,
        )
        if answer == QtWidgets.QMessageBox.StandardButton.Cancel:
            return False
        if answer == QtWidgets.QMessageBox.StandardButton.Save:
            return self.save_project()
        return True

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        """Persist window state and settings on exit."""
        self.settings.window_width = self.width()
        self.settings.window_height = self.height()
        self.settings.window_maximised = self.isMaximized()
        self.settings.active_tab = self.tabs.currentIndex()
        self.settings.default_mpi_ranks = self.aero_tab.ranks.value()
        save_settings(self.settings, self.store.root)
        self.ai_tab.save_conversation()
        event.accept()

    def _build_menu(self) -> None:
        """File, settings and help menus."""
        file_menu = self.menuBar().addMenu("&File")

        new_action = QtGui.QAction("&New project", self)
        new_action.setShortcut(QtGui.QKeySequence.StandardKey.New)
        new_action.triggered.connect(self.new_project)
        file_menu.addAction(new_action)

        open_action = QtGui.QAction("&Open project ...", self)
        open_action.setShortcut(QtGui.QKeySequence.StandardKey.Open)
        open_action.triggered.connect(lambda: self.open_project())
        file_menu.addAction(open_action)

        self.recent_menu = file_menu.addMenu("Open &recent")
        self._refresh_recent_menu()

        file_menu.addSeparator()

        save_action = QtGui.QAction("&Save project", self)
        save_action.setShortcut(QtGui.QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.save_project)
        file_menu.addAction(save_action)

        save_as_action = QtGui.QAction("Save project &as ...", self)
        save_as_action.setShortcut(QtGui.QKeySequence.StandardKey.SaveAs)
        save_as_action.triggered.connect(self.save_project_as)
        file_menu.addAction(save_as_action)

        details_action = QtGui.QAction("Project &details ...", self)
        details_action.triggered.connect(self.edit_project_details)
        file_menu.addAction(details_action)

        file_menu.addSeparator()

        demo = QtGui.QAction("Create sample &rocket CAD", self)
        demo.triggered.connect(self._create_sample_rocket)
        file_menu.addAction(demo)

        enclosure = QtGui.QAction("Create sample &enclosure CAD", self)
        enclosure.triggered.connect(self._create_sample_enclosure)
        file_menu.addAction(enclosure)

        file_menu.addSeparator()
        quit_action = QtGui.QAction("&Quit", self)
        quit_action.setShortcut(QtGui.QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        settings_menu = self.menuBar().addMenu("&Settings")
        preferences = QtGui.QAction("&Preferences ...", self)
        preferences.triggered.connect(self.edit_settings)
        settings_menu.addAction(preferences)

        open_data = QtGui.QAction("Open &data folder", self)
        open_data.triggered.connect(self._open_data_folder)
        settings_menu.addAction(open_data)

        help_menu = self.menuBar().addMenu("&Help")
        environment = QtGui.QAction("Check &environment", self)
        environment.triggered.connect(self._show_environment)
        help_menu.addAction(environment)

        documentation = QtGui.QAction("&Documentation", self)
        documentation.triggered.connect(self._open_documentation)
        help_menu.addAction(documentation)

    def _refresh_recent_menu(self) -> None:
        """Rebuild the recent-projects submenu.

        Only files still on disk are offered, so the menu never presents an
        entry that would immediately fail to open.
        """
        menu = getattr(self, "recent_menu", None)
        if menu is None:
            return
        menu.clear()

        existing = self.settings.existing_recent_projects()
        if not existing:
            empty = QtGui.QAction("(no recent projects)", self)
            empty.setEnabled(False)
            menu.addAction(empty)
            return

        for item in existing:
            action = QtGui.QAction(Path(item).name, self)
            action.setToolTip(item)
            action.triggered.connect(
                lambda _checked=False, target=item: self.open_project(target)
            )
            menu.addAction(action)

        menu.addSeparator()
        clear = QtGui.QAction("Clear list", self)
        clear.triggered.connect(self._clear_recent)
        menu.addAction(clear)

    def _clear_recent(self) -> None:
        """Forget every recent project."""
        self.settings.recent_projects = []
        save_settings(self.settings, self.store.root)
        self._refresh_recent_menu()

    def edit_settings(self) -> None:
        """Open the preferences dialog and apply the result."""
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.settings = dialog.updated_settings()
            save_settings(self.settings, self.store.root)
            self.aero_tab.ranks.setValue(self.settings.default_mpi_ranks)
            self.status.showMessage("Preferences saved", 4000)

    def _open_data_folder(self) -> None:
        """Reveal the data directory in the system file manager."""
        QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(str(self.store.root))
        )

    def _open_documentation(self) -> None:
        """Open the bundled documentation index."""
        docs = Path(__file__).resolve().parent.parent / "docs"
        target = docs / "README.md"
        if target.is_file():
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target)))
        else:  # pragma: no cover - docs always shipped
            QtWidgets.QMessageBox.information(
                self, APP_NAME, f"Documentation is in {docs}"
            )

    def _create_sample_rocket(self) -> None:
        """Generate the reference rocket and load it into the aero tab."""
        from backend.sample_geometry import create_reference_rocket_step

        path = self.store.root / "samples" / "reference_rocket.step"
        try:
            create_reference_rocket_step(path)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, APP_NAME, str(error))
            return
        self.aero_tab.step_path.setText(str(path))
        self.aero_tab._on_step_path_changed()
        self.aero_tab.append_log(f"Created sample rocket: {path}")
        self.tabs.setCurrentWidget(self.aero_tab)

    def _create_sample_enclosure(self) -> None:
        """Generate the reference sensor enclosure."""
        from backend.sample_geometry import create_sensor_enclosure_step

        path = self.store.root / "samples" / "sensor_enclosure.step"
        try:
            create_sensor_enclosure_step(path)
        except Exception as error:
            QtWidgets.QMessageBox.warning(self, APP_NAME, str(error))
            return
        self.sensor_tab.step_path.setText(str(path))
        self.tabs.setCurrentWidget(self.sensor_tab)

    def _report_environment(self) -> None:
        """Warn in the status bar when the solver stack is incomplete."""
        report = probe_environment(probe_versions=False)
        if report.solver_ready:
            self.status.showMessage("Solver stack ready", 6000)
        else:
            self.status.showMessage(
                "SU2 or MPI not found - meshing and analysis work, solving does "
                "not. See Help > Check environment.",
                12000,
            )

    def _show_environment(self) -> None:
        """Show the full environment report."""
        report = probe_environment(probe_versions=True)
        lines = [
            f"Operating system : {report.os_name}",
            f"Python           : {report.python_version}",
            f"Logical CPUs     : {report.cpu_count}",
            f"Suggested ranks  : {report.recommended_ranks}",
            f"Data directory   : {report.data_root}",
            f"MPI launcher     : {report.mpi_launcher or 'NOT FOUND'}",
            f"SU2_CFD          : {report.su2_cfd or 'NOT FOUND'}",
        ]
        if report.missing:
            lines += ["", "Missing:"] + [f"  - {item}" for item in report.missing]
            lines += ["", "Run 'python setup_env.py' to install them."]
        QtWidgets.QMessageBox.information(self, "Environment", "\n".join(lines))


def build_application(argv: list[str] | None = None) -> QtWidgets.QApplication:
    """Create the QApplication with the theme applied."""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(argv or [])
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET + _arrow_rules())
    return app


def _arrow_rules() -> str:
    """The combo-box arrow rules, or nothing if the images cannot be written.

    A read-only data folder must not stop the program starting; the arrows
    fall back to a temporary folder, and failing that are simply absent.
    """
    import tempfile

    from core.platform_env import data_root
    from gui.theme import arrow_stylesheet

    for directory in (data_root() / "ui", Path(tempfile.gettempdir()) / f"{APP_NAME}-ui"):
        try:
            return arrow_stylesheet(directory)
        except OSError:
            continue
    return ""


def run_gui(argv: list[str] | None = None) -> int:
    """Launch the desktop application."""
    app = build_application(argv)
    window = MainWindow()
    window.show()
    return app.exec()


def _auto_spin(
    low: float, high: float, decimals: int, step: float
) -> QtWidgets.QDoubleSpinBox:
    """A number box whose lowest position reads "Auto" and means None."""
    box = QtWidgets.QDoubleSpinBox()
    box.setDecimals(decimals)
    # One step below the real range is the Auto position.
    box.setRange(low - step, high)
    box.setSingleStep(step)
    box.setSpecialValueText("Auto")
    box.setProperty("floor", low)
    box.setValue(box.minimum())
    return box


def _auto_value(box: QtWidgets.QDoubleSpinBox) -> float | None:
    """None when the box shows Auto, otherwise its value.

    Anything typed below the real range counts as Auto too, rather than
    reaching the solver settings as a zero CFL.
    """
    floor = float(box.property("floor") or box.minimum())
    return None if box.value() < floor else box.value()


def _set_auto(box: QtWidgets.QDoubleSpinBox, value: float | None) -> None:
    """Show a value, or Auto for None."""
    box.setValue(box.minimum() if value is None else value)


def _opposite_axis(direction: str) -> str:
    """'+Y' for '-Y' and the reverse."""
    sign, axis = direction[0], direction[1:]
    return ("-" if sign == "+" else "+") + axis


def _wrap(layout: QtWidgets.QLayout) -> QtWidgets.QWidget:
    """Wrap a layout in a widget so it can go into a form row."""
    container = QtWidgets.QWidget()
    container.setLayout(layout)
    return container


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(run_gui(sys.argv))
