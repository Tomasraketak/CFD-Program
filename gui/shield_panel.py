"""Radiation-shield study: DoE, response surface and Monte Carlo in the GUI.

Every setting of :class:`core.shield_models.ShieldStudyParams` and
:class:`ShieldCfdSettings` has a control here, and the same study can be run
from the assistant through the ``radiation_shield_study`` tool. Studies are
stored as ``shield-...`` records, so either side can pick up the other's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets

from core.shield_models import (
    VARIABLE_LABELS,
    VARIABLE_NAMES,
    BottomMode,
    Distribution,
    DoeKind,
    ShieldCfdSettings,
    ShieldSetup,
    ShieldStudyParams,
    ShieldStudyResult,
    StudyVariable,
    SurrogateKind,
    VariableScale,
)
from gui.theme import CHART_COLOURS, DANGER, SUCCESS, WARNING

try:  # pragma: no cover - import guard
    import pyqtgraph as pg

    HAVE_CHARTS = True
except Exception:  # pragma: no cover - optional
    pg = None  # type: ignore[assignment]
    HAVE_CHARTS = False

VARIABLE_COLUMNS = ("Min", "Max", "Distribution", "Mean", "Std", "Mode", "Scale")


def _spin(low: float, high: float, value: float, decimals: int = 3, step: float = 0.1,
          suffix: str = "") -> QtWidgets.QDoubleSpinBox:
    box = QtWidgets.QDoubleSpinBox()
    box.setDecimals(decimals)
    box.setRange(low, high)
    box.setSingleStep(step)
    box.setValue(value)
    if suffix:
        box.setSuffix(suffix)
    return box


def _int_spin(low: int, high: int, value: int, step: int = 1) -> QtWidgets.QSpinBox:
    box = QtWidgets.QSpinBox()
    box.setRange(low, high)
    box.setSingleStep(step)
    box.setValue(value)
    return box


def _combo(values: list[str], current: str) -> QtWidgets.QComboBox:
    box = QtWidgets.QComboBox()
    for value in values:
        box.addItem(value, value)
    box.setCurrentIndex(max(0, values.index(current)))
    return box


def _optional_spin(high: float, decimals: int = 3) -> QtWidgets.QDoubleSpinBox:
    """A number box whose bottom position reads 'auto' and means None."""
    box = QtWidgets.QDoubleSpinBox()
    box.setDecimals(decimals)
    box.setRange(-1.0e6, high)
    box.setSpecialValueText("auto")
    box.setValue(box.minimum())
    return box


def _optional(box: QtWidgets.QDoubleSpinBox) -> float | None:
    return None if box.value() <= box.minimum() else box.value()


def _set_optional(box: QtWidgets.QDoubleSpinBox, value: float | None) -> None:
    box.setValue(box.minimum() if value is None else value)


class ShieldStudyPanel(QtWidgets.QWidget):
    """Controls, results and charts for radiation-shield studies."""

    statusMessage = QtCore.Signal(str)

    def __init__(self, store, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.pool = QtCore.QThreadPool.globalInstance()
        self.current_study: str | None = None
        self._workers: list[Any] = []

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
        splitter.setSizes([460, 900])
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self.refresh_studies()
        from gui.study_extras import publish_study_state

        self._publish_timer = publish_study_state(self, "shield")
        from gui.study_extras import wire_cfd_tools

        wire_cfd_tools(self, "shield", self.cfd_tools)

    # ------------------------------------------------------------------ controls

    def _build_controls(self) -> QtWidgets.QWidget:
        from gui.main_window import VectorInput

        defaults = ShieldStudyParams()
        setup = defaults.setup
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setSpacing(8)

        # --- geometry ---------------------------------------------------------
        group = QtWidgets.QGroupBox("Shield and domain")
        form = QtWidgets.QFormLayout(group)
        self.step_path = QtWidgets.QLineEdit()
        self.step_path.setPlaceholderText("Empty: built-in 20 cm multi-plate shield")
        browse = QtWidgets.QPushButton("Browse")
        browse.clicked.connect(self._browse)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.step_path, 1)
        row.addWidget(browse)
        self.step_path.editingFinished.connect(self._step_changed)
        form.addRow("Shield STEP", _wrap(row))
        self.scale = _optional_spin(1.0e3, decimals=6)
        self.scale.setToolTip("CAD unit multiplier; auto reads it from the file")
        form.addRow("Scale to metres", self.scale)
        self.shield_size = VectorInput(tuple(setup.shield_size_m))
        self.shield_size.setToolTip("Outer size of the shield [x along the wind, y, z up], m")
        form.addRow("Shield size", self.shield_size)
        self.plate_count = _int_spin(2, 20, setup.plate_count)
        form.addRow("Plates (built-in)", self.plate_count)
        self.domain_size = VectorInput(tuple(setup.domain_size_m))
        self.domain_size.setToolTip("Fluid domain [length along the wind, width, height], m")
        form.addRow("Domain", self.domain_size)
        self.thermometer = VectorInput(tuple(setup.thermometer_xyz_m))
        self.thermometer.setToolTip("Monitor point relative to the shield centre, m")
        form.addRow("Thermometer", self.thermometer)
        layout.addWidget(group)

        # --- baseline and surfaces -------------------------------------------
        group = QtWidgets.QGroupBox("Baseline condition and surfaces")
        form = QtWidgets.QFormLayout(group)
        self.ambient = _spin(-50.0, 60.0, setup.ambient_temp_c, 1, 1.0, " C")
        form.addRow("Inlet air", self.ambient)
        self.wind = _spin(0.01, 30.0, setup.wind_speed_ms, 2, 0.1, " m/s")
        form.addRow("Wind speed", self.wind)
        self.solar = _spin(0.0, 1500.0, setup.solar_flux_w_m2, 0, 50.0, " W/m2")
        form.addRow("Top solar", self.solar)
        self.bottom_mode = QtWidgets.QComboBox()
        self.bottom_mode.addItem("Ground: long-wave flux", BottomMode.GROUND_FLUX.value)
        self.bottom_mode.addItem("Vehicle roof: fixed temperature", BottomMode.ROOF_TEMPERATURE.value)
        form.addRow("Bottom", self.bottom_mode)
        self.bottom_flux = _spin(0.0, 1500.0, setup.bottom_flux_w_m2, 0, 25.0, " W/m2")
        form.addRow("Bottom flux", self.bottom_flux)
        self.bottom_temp = _spin(200.0, 450.0, setup.bottom_temperature_k, 1, 1.0, " K")
        form.addRow("Roof temperature", self.bottom_temp)
        self.roof_emissivity = _spin(0.01, 1.0, setup.roof_emissivity, 2, 0.01)
        form.addRow("Roof emissivity", self.roof_emissivity)
        self.sky = _optional_spin(600.0, decimals=1)
        self.sky.setToolTip("Downward sky long-wave, W/m2; auto = Swinbank, 0 = none")
        form.addRow("Sky long-wave", self.sky)
        self.albedo = _spin(0.0, 1.0, setup.ground_albedo, 2, 0.05)
        form.addRow("Ground albedo", self.albedo)
        self.absorptivity = _spin(0.0, 1.0, setup.shield_solar_absorptivity, 2, 0.05)
        form.addRow("Shield solar absorptivity", self.absorptivity)
        self.emissivity = _spin(0.0, 1.0, setup.shield_emissivity, 2, 0.05)
        form.addRow("Shield emissivity", self.emissivity)
        # Two-sided plates (shiny top, black bottom): 'auto' = the values above.
        self.top_absorptivity = _optional_spin(1.0, 2)
        self.top_emissivity = _optional_spin(1.0, 2)
        self.bottom_absorptivity = _optional_spin(1.0, 2)
        self.bottom_emissivity = _optional_spin(1.0, 2)
        for box in (self.top_absorptivity, self.top_emissivity,
                    self.bottom_absorptivity, self.bottom_emissivity):
            # One step below 0 reads 'auto'; the first step up is 0.00.
            box.setRange(-0.05, 1.0)
            box.setValue(-0.05)
            box.setSingleStep(0.05)
            box.setToolTip(
                "Faces looking up (top side) or down (bottom side); 'auto' "
                "uses the shield value above. E.g. shiny aluminium top 0.15 / "
                "0.1, black bottom 0.95 / 0.9."
            )
        self.optics_orientation = QtWidgets.QComboBox()
        self.optics_orientation.addItem("Up / down (+z / -z)", "vertical")
        self.optics_orientation.addItem("Outside / towards the thermometer", "thermometer")
        self.optics_orientation.setToolTip(
            "Up/down: 'top side' = faces looking up, 'bottom side' = faces looking "
            "down. Outside/towards the thermometer: 'top side' = the outside "
            "faces, 'bottom side' = the faces looking into the gaps towards the "
            "thermometer (e.g. black inside, shiny aluminium outside)."
        )
        form.addRow("Two-sided optics", self.optics_orientation)
        form.addRow("Top side absorptivity", self.top_absorptivity)
        form.addRow("Top side emissivity", self.top_emissivity)
        form.addRow("Bottom side absorptivity", self.bottom_absorptivity)
        form.addRow("Bottom side emissivity", self.bottom_emissivity)
        self.conductivity = _spin(0.01, 500.0, setup.shield_conductivity_w_mk, 3, 0.05, " W/(m K)")
        form.addRow("Shield conductivity", self.conductivity)
        self.ventilation = _spin(0.01, 1.0, setup.ventilation_coefficient, 2, 0.05)
        self.ventilation.setToolTip("Analytical model: inside air speed / wind speed")
        form.addRow("Ventilation coefficient", self.ventilation)
        self.bottom_mode.currentIndexChanged.connect(self._bottom_mode_changed)
        layout.addWidget(group)

        # --- uncertain inputs ------------------------------------------------
        group = QtWidgets.QGroupBox("Uncertain inputs (DoE and Monte Carlo)")
        box = QtWidgets.QVBoxLayout(group)
        self.variables = QtWidgets.QTableWidget(3, len(VARIABLE_COLUMNS))
        self.variables.setHorizontalHeaderLabels(list(VARIABLE_COLUMNS))
        self.variables.setVerticalHeaderLabels(
            [VARIABLE_LABELS[name].split(" [")[0] for name in VARIABLE_NAMES]
        )
        self.variables.setMinimumHeight(130)
        self._variable_widgets: dict[str, dict[str, QtWidgets.QWidget]] = {}
        for row, name in enumerate(VARIABLE_NAMES):
            variable = getattr(defaults, name)
            widgets = {
                "minimum": _spin(-1e6, 1e6, variable.minimum, 3, 0.1),
                "maximum": _spin(-1e6, 1e6, variable.maximum, 3, 0.1),
                "distribution": _combo([d.value for d in Distribution], variable.distribution.value),
                "mean": _optional_spin(1e6),
                "std": _optional_spin(1e6),
                "mode": _optional_spin(1e6),
                "scale": _combo([s.value for s in VariableScale], variable.scale.value),
            }
            for column, key in enumerate(widgets):
                self.variables.setCellWidget(row, column, widgets[key])
            self._variable_widgets[name] = widgets
        self.variables.resizeColumnsToContents()
        box.addWidget(self.variables)
        layout.addWidget(group)

        # --- study -------------------------------------------------------------
        group = QtWidgets.QGroupBox("Design exploration")
        form = QtWidgets.QFormLayout(group)
        self.doe = QtWidgets.QComboBox()
        self.doe.addItem("Central composite, face-centred (15 points)", DoeKind.CCD.value)
        self.doe.addItem("Latin hypercube", DoeKind.LHS.value)
        form.addRow("Design of experiments", self.doe)
        self.doe_points = _int_spin(6, 500, defaults.doe_points)
        form.addRow("LHS points", self.doe_points)
        self.surrogate = QtWidgets.QComboBox()
        self.surrogate.addItem("Full quadratic polynomial", SurrogateKind.QUADRATIC.value)
        self.surrogate.addItem("Radial basis (Kriging-like)", SurrogateKind.RBF.value)
        form.addRow("Response surface", self.surrogate)
        self.samples = _int_spin(100, 2_000_000, defaults.monte_carlo_samples, 1000)
        form.addRow("Monte Carlo samples", self.samples)
        self.tolerance = _spin(0.01, 20.0, defaults.tolerance_k, 2, 0.1, " K")
        form.addRow("Reliability tolerance", self.tolerance)
        self.seed = _int_spin(0, 2**31 - 1, defaults.seed)
        form.addRow("Random seed", self.seed)
        layout.addWidget(group)

        # --- CFD -----------------------------------------------------------------
        cfd = ShieldCfdSettings()
        group = QtWidgets.QGroupBox("SU2 CFD of the design points")
        form = QtWidgets.QFormLayout(group)
        self.resolution = _combo(["coarse", "medium", "fine"], cfd.mesh_resolution)
        form.addRow("Mesh", self.resolution)
        self.turbulence = _combo(["SST", "SA"], cfd.turbulence_model)
        self.turbulence.setToolTip(
            "SU2 has no standard k-epsilon; the Fluent package uses k-epsilon"
        )
        form.addRow("Turbulence model", self.turbulence)
        self.buoyancy = QtWidgets.QCheckBox("Gravity and natural convection")
        self.buoyancy.setChecked(cfd.buoyancy)
        form.addRow("", self.buoyancy)
        self.conduction = QtWidgets.QCheckBox("Heat conduction in the plates (conjugate)")
        self.conduction.setChecked(cfd.solid_conduction == "on")
        self.conduction.setToolTip(
            "Meshes the shield plates and solves conduction in them, coupled to "
            "SU2 pass by pass, so sun absorbed on a metal plate reaches its other "
            "faces. Off: every surface facet is an independent wall."
        )
        form.addRow("", self.conduction)
        self.passes = _int_spin(1, 20, cfd.radiation_passes)
        self.passes.setToolTip("Most passes; stops once the shield changes < 0.05 K")
        form.addRow("Passes (max)", self.passes)
        self.iter_first = _int_spin(50, 100_000, cfd.iterations_first_pass, 100)
        form.addRow("Iterations, first pass", self.iter_first)
        self.iter_later = _int_spin(50, 100_000, cfd.iterations_later_passes, 100)
        form.addRow("Iterations, later passes", self.iter_later)
        self.classes = _int_spin(2, 32, cfd.radiation_classes)
        form.addRow("Radiation markers", self.classes)
        self.rays = _int_spin(8, 1024, cfd.rays_per_face, 8)
        form.addRow("Rays per facet", self.rays)
        self.ranks = _int_spin(1, 256, cfd.mpi_ranks)
        form.addRow("MPI ranks", self.ranks)
        self.solve_limit = _int_spin(0, 500, 0)
        self.solve_limit.setSpecialValueText("all")
        self.solve_limit.setToolTip(
            "How many unsolved design points 'Solve CFD design points' runs "
            "this time; 1 solves just the centre point (DP0), e.g. to compare "
            "meshes. 'all' solves every one."
        )
        form.addRow("Points to solve now", self.solve_limit)
        layout.addWidget(group)

        # --- CFD point tools ---------------------------------------------------
        from gui.study_extras import CfdPointTools

        self.cfd_tools = CfdPointTools({
            "wind_speed_ms": ("Wind [m/s]", 0.05, 30.0, 0.5),
            "solar_flux_w_m2": ("Solar [W/m2]", 0.0, 1500.0, 1000.0),
            "bottom_flux_w_m2": ("Bottom [W/m2]", 0.0, 1500.0, 300.0),
        })
        layout.addWidget(self.cfd_tools)

        # --- sweep -------------------------------------------------------------
        from gui.study_extras import SweepControls

        self.sweep_controls = SweepControls(
            {name: VARIABLE_LABELS[name] for name in VARIABLE_NAMES},
            {
                "wind_speed_ms": (0.2, 5.0, 0.2),
                "solar_flux_w_m2": (0.0, 1200.0, 100.0),
                "bottom_flux_w_m2": (300.0, 800.0, 50.0),
            },
        )
        self.sweep_controls.run_button.clicked.connect(self.run_sweep)
        self.sweep_controls.preview_button.clicked.connect(self.draw_geometry)
        layout.addWidget(self.sweep_controls)

        # --- actions -------------------------------------------------------------
        self.run_button = QtWidgets.QPushButton("Run analytic study")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self.run_analytic)
        self.prepare_button = QtWidgets.QPushButton("Prepare CFD cases + Fluent package")
        self.prepare_button.clicked.connect(self.prepare_cfd)
        self.solve_button = QtWidgets.QPushButton("Solve CFD design points (SU2)")
        self.solve_button.clicked.connect(self.solve_cfd)
        self.import_button = QtWidgets.QPushButton("Import solved design points (CSV)...")
        self.import_button.clicked.connect(self.import_csv)
        self.open_button = QtWidgets.QPushButton("Open study folder")
        self.open_button.clicked.connect(self.open_folder)
        for button in (
            self.run_button, self.prepare_button, self.solve_button,
            self.import_button, self.open_button,
        ):
            layout.addWidget(button)
        layout.addStretch(1)
        self._bottom_mode_changed()
        return panel

    def _build_results(self) -> QtWidgets.QWidget:
        from gui.main_window import ResultCard

        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setSpacing(6)

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
        self.card_mean = ResultCard("Mean dT", "K")
        self.card_p95 = ResultCard("95 % of |dT| below", "K")
        self.card_worst = ResultCard("Worst |dT|", "K")
        self.card_reliability = ResultCard("Reliability", "%")
        self.card_fit = ResultCard("Surface error (LOO)", "K")
        for card in (
            self.card_mean, self.card_p95, self.card_worst,
            self.card_reliability, self.card_fit,
        ):
            cards.addWidget(card)
        layout.addLayout(cards)

        self.summary = QtWidgets.QLabel("Run a study to see the Monte Carlo results.")
        self.summary.setWordWrap(True)
        self.summary.setObjectName("hint")
        self.summary.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)

        charts = QtWidgets.QHBoxLayout()
        if HAVE_CHARTS:
            self.histogram = pg.PlotWidget(title="Monte Carlo: T_monitor - T_inlet")
            self.histogram.setLabel("bottom", "dT [K]")
            self.histogram.setLabel("left", "samples")
            self.response = pg.PlotWidget(title="Response surface: dT against wind")
            self.response.setLabel("bottom", "wind speed [m/s]")
            self.response.setLabel("left", "dT [K]")
            self.response.addLegend(offset=(-10, 10))
            charts.addWidget(self.histogram, 1)
            charts.addWidget(self.response, 1)
        else:  # pragma: no cover - optional dependency
            self.histogram = self.response = None
            charts.addWidget(QtWidgets.QLabel("pyqtgraph is not installed; charts disabled."))
        layout.addLayout(charts, 2)

        self.points_table = QtWidgets.QTableWidget(0, 5)
        self.points_table.setHorizontalHeaderLabels(
            ["Point", "Wind [m/s]", "Solar [W/m2]", "Bottom [W/m2]", "dT [K]"]
        )
        self.points_table.horizontalHeader().setStretchLastSection(True)
        from gui.study_extras import results_tabs

        self.result_tabs, self.sweep_view, self.geometry_view = results_tabs(self.points_table)
        layout.addWidget(self.result_tabs, 2)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        self.log.setMaximumHeight(140)
        layout.addWidget(self.log)
        return panel

    # ----------------------------------------------------------------- params

    def _bottom_mode_changed(self, *_: Any) -> None:
        roof = self.bottom_mode.currentData() == BottomMode.ROOF_TEMPERATURE.value
        self.bottom_temp.setEnabled(roof)
        self.bottom_flux.setEnabled(not roof)

    def _browse(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select the shield STEP file", "", "STEP files (*.step *.stp)"
        )
        if path:
            self.step_path.setText(path)
            self._step_changed()

    def _step_changed(self) -> None:
        """Read the new STEP's size into the form and redraw the picture.

        The analytic model does not read the file, so the size it uses has
        to come from somewhere: the file itself, rather than a 20 cm default
        the operator may not notice.
        """
        path = self.step_path.text().strip()
        if path and Path(path).is_file():
            from backend.shield_cfd import inspect_shield_step

            try:
                info = inspect_shield_step(path, _optional(self.scale))
            except Exception as error:  # noqa: BLE001 - shown to the operator
                self.append_log(f"Could not read {Path(path).name}: {error}")
                return
            self.shield_size.set_value(tuple(round(v, 4) for v in info["size_m"]))
            if info["solids"] >= 2:
                self.plate_count.setValue(min(20, info["solids"]))
            self.append_log(
                f"{Path(path).name}: {info['size_m'][0] * 1000:.1f} x "
                f"{info['size_m'][1] * 1000:.1f} x {info['size_m'][2] * 1000:.1f} mm, "
                f"{info['solids']} solid(s); shield size set from the file."
            )
        params = self._params_quietly()
        if params is not None:
            self._draw_geometry_for(params.setup)

    def _params_quietly(self) -> ShieldStudyParams | None:
        try:
            return self.study_params()
        except Exception:  # noqa: BLE001 - the form is mid-edit
            return None

    def study_params(self) -> ShieldStudyParams:
        """The study the form describes (raises on invalid values)."""
        setup = ShieldSetup(
            shield_step_path=self.step_path.text().strip(),
            scale_to_meters=_optional(self.scale),
            shield_size_m=self.shield_size.value(),
            plate_count=self.plate_count.value(),
            domain_size_m=self.domain_size.value(),
            thermometer_xyz_m=self.thermometer.value(),
            ambient_temp_c=self.ambient.value(),
            wind_speed_ms=self.wind.value(),
            solar_flux_w_m2=self.solar.value(),
            bottom_mode=BottomMode(self.bottom_mode.currentData()),
            bottom_flux_w_m2=self.bottom_flux.value(),
            bottom_temperature_k=self.bottom_temp.value(),
            roof_emissivity=self.roof_emissivity.value(),
            sky_longwave_w_m2=_optional(self.sky),
            ground_albedo=self.albedo.value(),
            shield_solar_absorptivity=self.absorptivity.value(),
            shield_emissivity=self.emissivity.value(),
            top_side_solar_absorptivity=_optional(self.top_absorptivity),
            top_side_emissivity=_optional(self.top_emissivity),
            bottom_side_solar_absorptivity=_optional(self.bottom_absorptivity),
            bottom_side_emissivity=_optional(self.bottom_emissivity),
            optics_orientation=self.optics_orientation.currentData(),
            shield_conductivity_w_mk=self.conductivity.value(),
            ventilation_coefficient=self.ventilation.value(),
        )
        variables = {}
        for name, widgets in self._variable_widgets.items():
            variables[name] = StudyVariable(
                minimum=widgets["minimum"].value(),
                maximum=widgets["maximum"].value(),
                distribution=Distribution(widgets["distribution"].currentData()),
                mean=_optional(widgets["mean"]),
                std=_optional(widgets["std"]),
                mode=_optional(widgets["mode"]),
                scale=VariableScale(widgets["scale"].currentData()),
            )
        return ShieldStudyParams(
            setup=setup,
            **variables,
            doe=DoeKind(self.doe.currentData()),
            doe_points=self.doe_points.value(),
            surrogate=SurrogateKind(self.surrogate.currentData()),
            monte_carlo_samples=self.samples.value(),
            tolerance_k=self.tolerance.value(),
            seed=self.seed.value(),
        )

    def cfd_settings(self) -> ShieldCfdSettings:
        """SU2 settings from the form."""
        return ShieldCfdSettings(
            mesh_resolution=self.resolution.currentData(),
            turbulence_model=self.turbulence.currentData(),
            buoyancy=self.buoyancy.isChecked(),
            radiation_passes=self.passes.value(),
            solid_conduction="on" if self.conduction.isChecked() else "off",
            iterations_first_pass=self.iter_first.value(),
            iterations_later_passes=self.iter_later.value(),
            radiation_classes=self.classes.value(),
            rays_per_face=self.rays.value(),
            mpi_ranks=self.ranks.value(),
        )

    def apply_params(self, params: ShieldStudyParams) -> None:
        """Show a stored study's settings in the form."""
        setup = params.setup
        self.step_path.setText(setup.shield_step_path)
        _set_optional(self.scale, setup.scale_to_meters)
        self.shield_size.set_value(tuple(setup.shield_size_m))
        self.plate_count.setValue(setup.plate_count)
        self.domain_size.set_value(tuple(setup.domain_size_m))
        self.thermometer.set_value(tuple(setup.thermometer_xyz_m))
        self.ambient.setValue(setup.ambient_temp_c)
        self.wind.setValue(setup.wind_speed_ms)
        self.solar.setValue(setup.solar_flux_w_m2)
        self.bottom_mode.setCurrentIndex(self.bottom_mode.findData(setup.bottom_mode.value))
        self.bottom_flux.setValue(setup.bottom_flux_w_m2)
        self.bottom_temp.setValue(setup.bottom_temperature_k)
        self.roof_emissivity.setValue(setup.roof_emissivity)
        _set_optional(self.sky, setup.sky_longwave_w_m2)
        self.albedo.setValue(setup.ground_albedo)
        self.absorptivity.setValue(setup.shield_solar_absorptivity)
        self.emissivity.setValue(setup.shield_emissivity)
        _set_optional(self.top_absorptivity, setup.top_side_solar_absorptivity)
        _set_optional(self.top_emissivity, setup.top_side_emissivity)
        _set_optional(self.bottom_absorptivity, setup.bottom_side_solar_absorptivity)
        _set_optional(self.bottom_emissivity, setup.bottom_side_emissivity)
        self.optics_orientation.setCurrentIndex(
            max(0, self.optics_orientation.findData(setup.optics_orientation))
        )
        self.conductivity.setValue(setup.shield_conductivity_w_mk)
        self.ventilation.setValue(setup.ventilation_coefficient)
        for name, widgets in self._variable_widgets.items():
            variable = getattr(params, name)
            widgets["minimum"].setValue(variable.minimum)
            widgets["maximum"].setValue(variable.maximum)
            widgets["distribution"].setCurrentIndex(
                widgets["distribution"].findData(variable.distribution.value)
            )
            _set_optional(widgets["mean"], variable.mean)
            _set_optional(widgets["std"], variable.std)
            _set_optional(widgets["mode"], variable.mode)
            widgets["scale"].setCurrentIndex(widgets["scale"].findData(variable.scale.value))
        self.doe.setCurrentIndex(self.doe.findData(params.doe.value))
        self.doe_points.setValue(params.doe_points)
        self.surrogate.setCurrentIndex(self.surrogate.findData(params.surrogate.value))
        self.samples.setValue(params.monte_carlo_samples)
        self.tolerance.setValue(params.tolerance_k)
        self.seed.setValue(params.seed)

    # ---------------------------------------------------------------- actions

    def _params_or_warn(self) -> ShieldStudyParams | None:
        try:
            return self.study_params()
        except Exception as error:  # noqa: BLE001 - shown to the operator
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
        for button in (
            self.run_button, self.prepare_button, self.solve_button, self.import_button,
        ):
            button.setEnabled(not busy)

    def _failed(self, message: str) -> None:
        self._set_busy(False)
        self.append_log(message)
        QtWidgets.QMessageBox.warning(self, "Radiation shield study", message.split("\n\n")[0])

    def append_log(self, line: str) -> None:
        """Add a line to the study log."""
        self.log.appendPlainText(line.rstrip())

    def run_analytic(self) -> None:
        """DoE + analytical model + response surface + Monte Carlo."""
        params = self._params_or_warn()
        if params is None:
            return
        from backend import shield_workflow

        self._start(
            lambda progress: shield_workflow.run_analytic(self.store, params),
            self._show_result,
            "Running the analytic study ...",
        )

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
                sweeps.shield_sweep(params.setup, variable, start, stop, step),
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
        """Draw the shield the study meshes, with the sun, bottom and wind."""
        params = self._params_or_warn()
        if params is None:
            return
        self._draw_geometry_for(params.setup, switch=True)

    def _redraw_if_changed(self, setup) -> None:
        """Draw the geometry again when it is not the one on screen."""
        from backend.study_preview import preview_key

        if getattr(self.geometry_view, "key", None) != preview_key("shield", setup):
            self._draw_geometry_for(setup)

    def _draw_geometry_for(self, setup, switch: bool = False) -> None:
        from backend.study_preview import cached_preview
        from gui.study_extras import run_in_background

        from backend.study_preview import preview_key

        key = preview_key("shield", setup)
        self._geometry_request = key

        def done(path) -> None:
            # A slower, older drawing must not replace a newer one.
            if key != self._geometry_request:
                return
            self.geometry_view.show_image(path)
            self.geometry_view.key = key
            if switch:
                self.result_tabs.setCurrentWidget(self.geometry_view)

        run_in_background(
            self, lambda: cached_preview("shield", setup), done, "Drawing the shield geometry ..."
        )

    def run_analytic_now(self) -> ShieldStudyResult | None:
        """The same on the calling thread (tests, scripting)."""
        params = self._params_or_warn()
        if params is None:
            return None
        from backend import shield_workflow

        result = shield_workflow.run_analytic(self.store, params)
        self._show_result(result)
        return result

    def prepare_cfd(self) -> None:
        """Mesh, ray-cast, write SU2 cases and the Fluent package."""
        params = self._params_or_warn()
        if params is None:
            return
        try:
            cfd = self.cfd_settings()
        except Exception as error:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Invalid settings", str(error))
            return
        from backend import shield_workflow

        def done(prepared: dict) -> None:
            self.current_study = prepared["study_id"]
            self.refresh_studies(select=prepared["study_id"])
            self.append_log(
                f"Prepared {prepared['design_points']} design points on "
                f"{prepared['cell_count']:,} cells in {prepared['cfd_folder']}\n"
                f"Fluent package: {prepared['fluent_folder']}\n"
                f"Solve here with 'Solve CFD design points', or run "
                f"{Path(prepared['run_script_windows']).name} on the solving computer."
            )

        self._start(
            lambda progress: shield_workflow.prepare_cfd(self.store, params, cfd, progress),
            done,
            "Preparing the CFD cases (mesh, radiation rays, Fluent package) ...",
        )

    def solve_cfd(self) -> None:
        """Solve the selected study's unsolved design points with SU2."""
        study = self.study_list.currentData()
        if not study:
            QtWidgets.QMessageBox.information(
                self, "Radiation shield study", "Prepare the CFD cases first."
            )
            return
        from backend import shield_workflow
        from backend.runner import SU2Runner

        limit = self.solve_limit.value() or None

        def done(summary: dict) -> None:
            for failure in summary.get("failures", []):
                self.append_log(f"FAILED {failure}")
            self.append_log(f"{summary['solved']} of {summary['total']} design points solved")
            if summary.get("result") is not None:
                self._show_result(summary["result"])
            elif summary.get("analysis_pending"):
                self.append_log(summary["analysis_pending"])

        self._start(
            lambda progress: shield_workflow.solve_cfd(
                self.store, study, SU2Runner(), progress, max_points=limit
            ),
            done,
            f"Solving the design points of {study} with SU2 ...",
        )

    def import_csv(self) -> None:
        """Analyse design points solved elsewhere (Fluent, a colleague's PC)."""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Solved design points", "", "CSV files (*.csv)"
        )
        if not path:
            return
        params = self._params_or_warn()
        if params is None:
            return
        from backend import shield_workflow

        try:
            result = shield_workflow.analyse_imported(self.store, params, path)
        except Exception as error:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Import failed", str(error))
            return
        self._show_result(result)

    def open_folder(self) -> None:
        """Open the selected study's folder in the file manager."""
        study = self.study_list.currentData()
        if not study:
            return
        folder = self.store.get(study).directory
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder)))

    # ---------------------------------------------------------------- results

    def refresh_studies(self, select: str | None = None) -> None:
        """List stored studies, newest first."""
        current = select or self.study_list.currentData()
        self.study_list.blockSignals(True)
        self.study_list.clear()
        for record in self.store.list_records("shield"):
            label = record.metadata.get("label", "")
            worst = record.metadata.get("worst_abs_delta_t_k")
            text = record.record_id + (f"  ({label})" if label else "")
            if worst is not None:
                text += f"  worst |dT| {worst:.2f} K"
            self.study_list.addItem(text, record.record_id)
        index = self.study_list.findData(current) if current else -1
        self.study_list.setCurrentIndex(index if index >= 0 else (0 if self.study_list.count() else -1))
        self.study_list.blockSignals(False)

    def _study_selected(self, *_: Any) -> None:
        study = self.study_list.currentData()
        if not study:
            return
        from backend import shield_workflow

        try:
            self.apply_params(shield_workflow.load_params(self.store, study))
            result = shield_workflow.load_result(self.store, study)
        except Exception as error:  # noqa: BLE001
            self.append_log(f"Could not open {study}: {error}")
            return
        self.current_study = study
        if result is not None:
            self._show_result(result, refresh=False)

    def _show_result(self, result: ShieldStudyResult, refresh: bool = True) -> None:
        mc = result.monte_carlo
        self.card_mean.set_value(mc.mean_k, "{:+.3f}")
        self.card_p95.set_value(mc.abs_p95_k, "{:.3f}")
        self.card_worst.set_value(mc.abs_max_k, "{:.3f}")
        self.card_reliability.set_value(100.0 * mc.reliability, "{:.1f}")
        self.card_fit.set_value(result.surrogate.loo_rmse_k, "{:.3f}")
        colour = SUCCESS if mc.reliability >= 0.95 else (WARNING if mc.reliability >= 0.8 else DANGER)
        self.card_reliability.set_colour(colour)
        worst = mc.worst_case
        lines = [
            f"{result.evaluator.value} design points: {len(result.design_points)}; "
            f"surface {result.surrogate.kind.value}, R2 {result.surrogate.r_squared:.4f}; "
            f"{mc.samples:,} Monte Carlo samples.",
            f"Worst case dT {worst['delta_t_k']:+.3f} K at wind {worst['wind_speed_ms']:.2f} m/s, "
            f"solar {worst['solar_flux_w_m2']:.0f} W/m2, bottom {worst['bottom_flux_w_m2']:.0f} W/m2"
            + (
                f" (re-solved directly: {result.worst_case_check_k:+.3f} K)"
                if result.worst_case_check_k is not None
                else ""
            )
            + ".",
            f"P(|dT| <= {mc.tolerance_k:g} K) = {100 * mc.reliability:.1f} %; "
            f"dT 5-95 %: {mc.p05_k:+.3f} .. {mc.p95_k:+.3f} K.",
            "Sensitivity (rank correlation): "
            + ", ".join(
                f"{VARIABLE_LABELS[name].split(' [')[0].lower()} {value:+.2f}"
                for name, value in mc.sensitivities.items()
            ),
        ]
        lines += result.notes
        self.summary.setText("\n".join(lines))

        self.points_table.setRowCount(len(result.design_points))
        for row, point in enumerate(result.design_points):
            values = [
                point.name,
                f"{point.wind_speed_ms:.3g}",
                f"{point.solar_flux_w_m2:.4g}",
                f"{point.bottom_flux_w_m2:.4g}",
                "" if point.delta_t_k is None else f"{point.delta_t_k:+.4f}",
            ]
            for column, value in enumerate(values):
                self.points_table.setItem(row, column, QtWidgets.QTableWidgetItem(value))
        self._draw_charts(result)
        if self.isVisible():
            self._redraw_if_changed(result.params.setup)
        if refresh and result.study_id:
            self.refresh_studies(select=result.study_id)
        self.statusMessage.emit(
            f"Shield study: worst |dT| {mc.abs_max_k:.2f} K, reliability {100 * mc.reliability:.1f} %"
        )

    def _draw_charts(self, result: ShieldStudyResult) -> None:
        if self.histogram is None:
            return
        import numpy as np

        from backend.shield_study import ResponseSurface

        mc = result.monte_carlo
        self.histogram.clear()
        edges = np.array(mc.histogram_edges_k)
        bars = pg.BarGraphItem(
            x0=edges[:-1], x1=edges[1:], height=mc.histogram_counts,
            brush=CHART_COLOURS[0],
        )
        self.histogram.addItem(bars)
        for limit in (-mc.tolerance_k, mc.tolerance_k):
            self.histogram.addItem(pg.InfiniteLine(limit, pen=pg.mkPen(DANGER, style=QtCore.Qt.PenStyle.DashLine)))

        self.response.clear()
        try:
            surface = ResponseSurface(result.params, result.design_points)
        except Exception:  # noqa: BLE001 - nothing to draw yet
            return
        params = result.params
        wind = params.wind_speed_ms
        solar = params.solar_flux_w_m2
        bottom = params.bottom_flux_w_m2
        speeds = np.geomspace(wind.minimum, wind.maximum, 60)
        for colour, (label, s_value, b_value) in zip(
            CHART_COLOURS,
            (
                ("low sun, low bottom", solar.minimum, bottom.minimum),
                ("mid", 0.5 * (solar.minimum + solar.maximum), 0.5 * (bottom.minimum + bottom.maximum)),
                ("high sun, high bottom", solar.maximum, bottom.maximum),
            ),
        ):
            inputs = np.column_stack(
                [speeds, np.full_like(speeds, s_value), np.full_like(speeds, b_value)]
            )
            self.response.plot(speeds, surface.predict(inputs), pen=pg.mkPen(colour, width=2), name=label)
        solved = [p for p in result.design_points if p.delta_t_k is not None]
        self.response.plot(
            [p.wind_speed_ms for p in solved], [p.delta_t_k for p in solved],
            pen=None, symbol="o", symbolBrush=CHART_COLOURS[3], name="design points",
        )


def _wrap(layout: QtWidgets.QLayout) -> QtWidgets.QWidget:
    container = QtWidgets.QWidget()
    container.setLayout(layout)
    return container

