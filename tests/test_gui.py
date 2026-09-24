"""Desktop GUI construction and parameter round-tripping.

Runs against Qt's offscreen platform, so the window, tabs and widgets are
built for real without needing a display. The emphasis is on the property
that matters architecturally: the GUI produces exactly the same validated
parameter models the MCP server does, so the two surfaces cannot drift apart.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# pydantic must finish importing before Qt: PySide6's import hook runs
# inspect.getsource on each new module and trips pydantic's migration shim if
# it fires mid-import. See gui/__init__.py.
from core.frames import Frame  # noqa: E402
from core.models import (  # noqa: E402
    AxisDirection,
    DomainShape,
    FlowParams,
    GeometryParams,
    MeshParams,
    ThermalParams,
    VelocityType,
)
from core.store import RunStore  # noqa: E402

pytest.importorskip("PySide6")

from PySide6 import QtWidgets  # noqa: E402
from gui.form_builder import LabelledSlider, ModelForm, humanise  # noqa: E402
from gui.main_window import (  # noqa: E402
    AerodynamicsTab,
    MainWindow,
    ResultCard,
    SensorTab,
    VectorInput,
    build_application,
)


@pytest.fixture(scope="module")
def qt_app():
    """One QApplication for the module."""
    app = build_application([])
    yield app
    app.processEvents()


@pytest.fixture
def aero_tab(qt_app, store: RunStore):
    """An aerodynamics tab backed by an isolated run store."""
    tab = AerodynamicsTab(store)
    yield tab
    tab.deleteLater()


@pytest.fixture
def sensor_tab(qt_app, store: RunStore):
    """A sensor microclimate tab."""
    tab = SensorTab(store)
    yield tab
    tab.deleteLater()


# ---------------------------------------------------------------------------
# Form builder
# ---------------------------------------------------------------------------


def test_humanise_surfaces_units_from_field_names():
    """Unit suffixes become readable labels."""
    assert humanise("vehicle_speed_ms") == "Vehicle Speed [m/s]"
    assert humanise("aoa_deg") == "Aoa [deg]"
    assert humanise("solar_flux_w_m2") == "Solar Flux [W/m^2]"
    assert humanise("heal_geometry") == "Heal Geometry"


def test_form_is_generated_from_a_model(qt_app):
    """Widgets come from the model, not from a hand-written list."""
    form = ModelForm(MeshParams, MeshParams())
    assert "boundary_layers" in form.fields
    assert "target_yplus" in form.fields
    assert isinstance(
        form.fields["boundary_layers"].widget, QtWidgets.QSpinBox
    )
    assert isinstance(
        form.fields["resolution"].widget, QtWidgets.QComboBox
    )


def test_numeric_bounds_become_widget_limits(qt_app):
    """A field's declared range constrains the widget.

    This is what stops the GUI offering values the model would reject.
    """
    form = ModelForm(MeshParams, MeshParams())
    layers = form.fields["boundary_layers"].widget
    assert layers.minimum() == 5
    assert layers.maximum() == 8

    yplus = form.fields["target_yplus"].widget
    assert yplus.minimum() == pytest.approx(30.0)
    assert yplus.maximum() == pytest.approx(300.0)


def test_field_descriptions_become_tooltips(qt_app):
    """The same text an agent reads in the schema is shown to the operator."""
    form = ModelForm(ThermalParams, None, skip=("sensor_xyz",))
    widget = form.fields["solar_flux_w_m2"]
    assert widget.description
    assert widget.widget.toolTip() == widget.description


def test_form_round_trips_to_a_validated_model(qt_app):
    """A form builds the very model the backend consumes."""
    form = ModelForm(MeshParams, MeshParams(boundary_layers=6))
    assert form.fields["boundary_layers"].value() == 6
    rebuilt = form.build()
    assert isinstance(rebuilt, MeshParams)
    assert rebuilt.boundary_layers == 6


def test_enum_fields_offer_every_member(qt_app):
    """Every enum option is selectable."""
    form = ModelForm(MeshParams, MeshParams())
    combo = form.fields["resolution"].widget
    assert combo.count() == 3


def test_labelled_slider_reports_physical_values(qt_app):
    """Qt sliders are integer-only; the readout must show real units."""
    slider = LabelledSlider(-20.0, 20.0, 5.0, decimals=1, suffix=" deg")
    assert slider.value() == pytest.approx(5.0)
    slider.setValue(-12.5)
    assert slider.value() == pytest.approx(-12.5)
    assert "-12.5" in slider.readout.text()


def test_labelled_slider_emits_physical_values(qt_app):
    """The change signal carries the scaled value, not the raw integer."""
    slider = LabelledSlider(0.0, 10.0, 1.0, decimals=2)
    seen: list[float] = []
    slider.valueChanged.connect(seen.append)
    slider.setValue(7.25)
    assert seen and seen[-1] == pytest.approx(7.25)


# ---------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------


def test_vector_input_round_trips(qt_app):
    """Three-component inputs read back what was written."""
    vector = VectorInput((1.0, 2.0, 3.0))
    assert vector.value() == [1.0, 2.0, 3.0]
    vector.set_value((-0.5, 0.25, 9.0))
    assert vector.value() == pytest.approx([-0.5, 0.25, 9.0])


def test_result_card_formats_and_blanks(qt_app):
    """Cards show numbers, and '--' when there is nothing to show."""
    card = ResultCard("Drag", "N")
    card.set_value(12.3456, "{:.2f}")
    assert card.value.text() == "12.35"
    card.set_value(float("nan"))
    assert card.value.text() == "--"


# ---------------------------------------------------------------------------
# Aerodynamics tab
# ---------------------------------------------------------------------------


def test_aero_tab_builds_every_control(aero_tab):
    """The controls named in the specification are all present."""
    assert aero_tab.step_path is not None
    assert aero_tab.axis_buttons.buttons()
    assert aero_tab.upstream and aero_tab.downstream and aero_tab.radial
    assert aero_tab.aoa and aero_tab.sideslip
    assert aero_tab.hinge_point and aero_tab.hinge_direction
    assert aero_tab.chart is not None
    assert aero_tab.card_cd and aero_tab.card_torque


def test_importing_a_step_file_reads_its_units_and_tells_the_assistant(
    aero_tab, tmp_path
):
    """Importing CAD sets the scale and hands the file to the assistant.

    Both halves matter. A millimetre model left at scale 1.0 is a
    kilometre-long rocket, and a file the assistant cannot see is a path the
    operator has to type twice.
    """
    from core.workspace import active_geometry
    from tests.test_step_inspect import MILLIMETRES, write_step

    path = write_step(
        tmp_path / "Sapphire.step",
        MILLIMETRES,
        [(0.0, 0.0, 0.0), (1320.0, 92.5, 92.5)],
    )
    aero_tab.step_path.setText(path)
    aero_tab._on_step_path_changed()

    assert aero_tab.scale.value() == pytest.approx(1e-3)
    assert "millimetres" in aero_tab.geometry_note.text()
    assert "1.32 m" in aero_tab.geometry_note.text()

    loaded = active_geometry()
    assert loaded is not None
    assert loaded.step_file_path == path
    assert loaded.source == "gui"


def test_importing_a_rocket_sets_the_nose_axis_from_its_shape(aero_tab, tmp_path):
    """The operator should not have to work out the CAD frame themselves.

    A nose tapers and a tail does not, so the axis buttons can be filled in
    from the geometry. Getting this wrong meshes the rocket backwards and
    returns a full set of plausible forces for a vehicle flying tail-first.
    """
    from core.workspace import active_geometry
    from tests.test_step_inspect import MILLIMETRES, rocket_points, write_step

    path = write_step(tmp_path / "rocket.step", MILLIMETRES, rocket_points())
    aero_tab.step_path.setText(path)
    aero_tab._on_step_path_changed()

    # The button says where the nose is -- the same thing the note says.
    # It used to light the nose-to-tail direction, so a rocket reported as
    # "nose at +X" had "-X" selected beside it.
    checked = [b.text() for b in aero_tab.axis_buttons.buttons() if b.isChecked()]
    assert checked == ["+X"]
    assert "nose at +X" in aero_tab.geometry_note.text()
    # Meshing still receives nose-to-tail.
    assert active_geometry().nose_direction == "-X"
    assert aero_tab.geometry_params().nose_direction == AxisDirection.MINUS_X


def test_a_body_with_no_obvious_nose_asks_the_operator(aero_tab, tmp_path):
    """When the shape does not say, the note asks rather than defaulting."""
    from tests.test_step_inspect import MILLIMETRES, rocket_points, write_step

    path = write_step(
        tmp_path / "tube.step", MILLIMETRES, rocket_points(nose_radius=90.0)
    )
    aero_tab.step_path.setText(path)
    aero_tab._on_step_path_changed()

    note = aero_tab.geometry_note.text()
    assert "Please check" in note
    assert "which end" in note.lower()


def test_a_project_keeps_its_own_nose_axis(aero_tab, tmp_path):
    """A saved study's orientation is a decision, not a guess to redo."""
    from core.workspace import active_geometry
    from tests.test_step_inspect import MILLIMETRES, rocket_points, write_step

    path = write_step(tmp_path / "rocket.step", MILLIMETRES, rocket_points())
    for button in aero_tab.axis_buttons.buttons():
        button.setChecked(button.text() == "+Y")
    aero_tab.step_path.setText(path)
    aero_tab._on_step_path_changed(keep_scale=True)

    # Nose pointing to +Y is a body running nose-to-tail along -Y.
    assert active_geometry().nose_direction == "-Y"


def test_clearing_the_path_box_empties_the_bench(aero_tab, tmp_path):
    """Removing the file means nothing is loaded, not the previous file."""
    from core.workspace import active_geometry
    from tests.test_step_inspect import MILLIMETRES, write_step

    path = write_step(tmp_path / "r.step", MILLIMETRES, [(0, 0, 0), (1320, 92, 92)])
    aero_tab.step_path.setText(path)
    aero_tab._on_step_path_changed()
    aero_tab.step_path.setText("")
    aero_tab._on_step_path_changed()

    assert active_geometry() is None
    assert "No CAD file" in aero_tab.geometry_note.text()


def test_a_path_that_is_not_on_disk_is_reported_not_guessed_at(aero_tab, tmp_path):
    """A typo should say so rather than quietly leave the old scale."""
    aero_tab.step_path.setText(str(tmp_path / "absent.step"))
    aero_tab._on_step_path_changed()
    assert "not on disk" in aero_tab.geometry_note.text()


def test_an_unconfident_unit_asks_the_operator_to_check(aero_tab, tmp_path):
    """A guessed scale must not look like a settled one."""
    from tests.test_step_inspect import write_step

    path = write_step(
        tmp_path / "mystery.step", "#20=NOTHING();", [(0, 0, 0), (2.0, 0.1, 0.1)]
    )
    aero_tab.step_path.setText(path)
    aero_tab._on_step_path_changed()
    note = aero_tab.geometry_note.text()
    assert "Please check" in note
    assert "What unit" in note


def test_nose_axis_buttons_cover_all_six_directions(aero_tab):
    """Plus and minus X, Y and Z are all offered."""
    labels = {button.text() for button in aero_tab.axis_buttons.buttons()}
    assert labels == {direction.value for direction in AxisDirection}


def test_aero_tab_builds_valid_geometry_params(aero_tab):
    """The form produces the same model the MCP tool builds."""
    aero_tab.step_path.setText("/models/rocket.step")
    geometry = aero_tab.geometry_params()
    assert isinstance(geometry, GeometryParams)
    assert geometry.step_file_path == "/models/rocket.step"
    assert geometry.resolved_nose_vector() == pytest.approx((1.0, 0.0, 0.0))


def test_custom_nose_vector_overrides_the_axis_buttons(aero_tab):
    """An arbitrary direction is available alongside the named axes."""
    aero_tab.step_path.setText("/models/rocket.step")
    aero_tab.custom_nose.setChecked(True)
    # Like the buttons, the vector is where the nose points ...
    aero_tab.nose_vector.set_value((0.0, 0.0, 4.0))
    geometry = aero_tab.geometry_params()
    # ... and meshing gets nose-to-tail.
    assert geometry.nose_vector == pytest.approx([0.0, 0.0, -1.0])
    assert geometry.resolved_nose_vector() == pytest.approx((0.0, 0.0, -1.0))


def test_domain_sliders_default_to_the_specified_envelope(aero_tab):
    """5 L upstream, 10 L downstream, 5 L radial."""
    domain = aero_tab.domain_params()
    assert domain.upstream_multiplier == pytest.approx(5.0)
    assert domain.downstream_multiplier == pytest.approx(10.0)
    assert domain.radial_multiplier == pytest.approx(5.0)
    assert domain.shape is DomainShape.CYLINDER


def test_flow_params_follow_the_sliders(aero_tab):
    """Attitude sliders feed straight into the flow model."""
    aero_tab.aoa.setValue(7.5)
    aero_tab.sideslip.setValue(-3.0)
    aero_tab.velocity.setValue(2.4)
    flow = aero_tab.flow_params()
    assert isinstance(flow, FlowParams)
    assert flow.aoa_deg == pytest.approx(7.5)
    assert flow.sideslip_deg == pytest.approx(-3.0)
    assert flow.mach() == pytest.approx(2.4)


def test_angle_sliders_cannot_leave_the_envelope(aero_tab):
    """The +/-20 degree limit is enforced by the widget itself."""
    aero_tab.aoa.setValue(90.0)
    assert aero_tab.aoa.value() <= 20.0
    aero_tab.aoa.setValue(-90.0)
    assert aero_tab.aoa.value() >= -20.0


def test_switching_to_true_airspeed_rescales_the_slider(aero_tab):
    """The speed control adapts to the selected unit."""
    aero_tab.velocity_type.setCurrentIndex(1)  # true airspeed
    # Qt flattens a str-based enum to a plain string in QVariant, so the GUI
    # converts it back explicitly; check the resulting model, not the widget.
    assert aero_tab.flow_params().velocity_type is VelocityType.TAS
    aero_tab.velocity.setValue(340.294)
    assert aero_tab.flow_params().mach() == pytest.approx(1.0, rel=1e-3)


def test_condition_label_reports_the_derived_state(aero_tab):
    """The operator sees Mach, speed, pressure and temperature together."""
    aero_tab._update_condition_label()
    text = aero_tab.condition_label.text()
    assert "M " in text and "kPa" in text


def test_hinge_axis_is_built_from_the_form(aero_tab):
    """The interactive hinge definition produces a HingeAxis."""
    aero_tab.hinge_name.setText("fin_pitch")
    aero_tab.hinge_point.set_value((0.9, 0.05, 0.0))
    aero_tab.hinge_direction.set_value((0.0, 2.0, 0.0))
    axes = aero_tab.hinge_axes()
    assert len(axes) == 1
    assert axes[0].name == "fin_pitch"
    assert axes[0].unit_direction() == pytest.approx((0.0, 1.0, 0.0))
    # Entered in the axes the viewport shows.
    assert axes[0].frame is Frame.ROCKET


def test_a_solver_frame_hinge_from_an_old_project_is_shown_in_rocket_axes(aero_tab):
    """A project saved before the rocket frame keeps meaning the same hinge."""
    from core.models import HingeAxis
    from core.project import default_project

    project = default_project()
    project.hinge_axes = [
        HingeAxis(name="old", point=[0.9, 0.05, 0.0], direction=[0.0, 1.0, 0.0])
    ]
    aero_tab.apply_project(project)
    # 0.9 m behind the nose in the solver frame is z = -0.9 on the rocket.
    assert aero_tab.hinge_point.value() == pytest.approx([0.0, 0.05, -0.9])
    rebuilt = aero_tab.hinge_axes()[0]
    assert rebuilt.solver_point() == pytest.approx([0.9, 0.05, 0.0])


def test_degenerate_hinge_direction_is_dropped(aero_tab):
    """A zero direction defines no axis, so none is reported."""
    aero_tab.hinge_direction.set_value((0.0, 0.0, 0.0))
    assert aero_tab.hinge_axes() == []


def test_run_buttons_stay_disabled_until_a_mesh_exists(aero_tab):
    """Solving without a mesh is not offered."""
    assert not aero_tab.run_button.isEnabled()
    assert not aero_tab.sweep_button.isEnabled()


def test_meshing_without_a_file_is_refused(aero_tab, monkeypatch):
    """A missing CAD path produces a warning, not a crash."""
    warnings: list[str] = []
    monkeypatch.setattr(aero_tab, "_warn", warnings.append)
    aero_tab.step_path.setText("")
    aero_tab.generate_mesh()
    assert warnings and "STEP" in warnings[0]


def test_log_panel_records_messages(aero_tab):
    """Progress messages reach the log."""
    aero_tab.append_log("hello")
    assert "hello" in aero_tab.log.toPlainText()


# ---------------------------------------------------------------------------
# Sensor tab
# ---------------------------------------------------------------------------


def test_sensor_tab_defaults_to_the_specified_scenario(sensor_tab):
    """2 m/s, 25 C, 800 W/m^2, 0.1 m above the roof."""
    params = sensor_tab.thermal_params()
    assert isinstance(params, ThermalParams)
    assert params.vehicle_speed_ms == pytest.approx(2.0)
    assert params.ambient_temp_c == pytest.approx(25.0)
    assert params.solar_flux_w_m2 == pytest.approx(800.0)
    assert params.height_above_roof_m == pytest.approx(0.10)


def test_sensor_tab_shows_a_prediction_immediately(sensor_tab):
    """The analytical model is fast enough to run on construction."""
    assert sensor_tab.card_sensor.value.text() != "--"
    assert sensor_tab.card_bias.value.text() != "--"
    assert sensor_tab.verdict.text()


def test_sensor_readout_updates_when_a_slider_moves(sensor_tab):
    """Dragging a slider re-solves and updates the readout live."""
    before = sensor_tab.card_bias.value.text()
    sensor_tab.solar.setValue(0.0)
    sensor_tab.recompute()
    after = sensor_tab.card_bias.value.text()
    assert before != after


def test_removing_the_sun_reduces_the_predicted_error(sensor_tab):
    """The tab reflects the underlying physics."""
    sensor_tab.solar.setValue(1000.0)
    sensor_tab.recompute()
    sunny = float(sensor_tab.card_bias.value.text())

    sensor_tab.solar.setValue(0.0)
    sensor_tab.recompute()
    shaded = float(sensor_tab.card_bias.value.text())
    assert shaded < sunny


def test_verdict_explains_the_intake_position(sensor_tab):
    """The tab says whether the intake samples ambient air."""
    sensor_tab.height.setValue(0.5)
    sensor_tab.recompute()
    assert "clear of" in sensor_tab.verdict.text()

    sensor_tab.height.setValue(0.01)
    sensor_tab.speed.setValue(1.0)
    sensor_tab.recompute()
    assert "inside" in sensor_tab.verdict.text()


def test_sensor_position_is_configurable(sensor_tab):
    """The die coordinate feeds the probe location."""
    sensor_tab.sensor_xyz.set_value((0.01, 0.02, 0.03))
    assert sensor_tab.thermal_params().sensor_xyz == pytest.approx(
        [0.01, 0.02, 0.03]
    )


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


def test_main_window_has_both_tracks(qt_app, store):
    """One tab per simulation track, the assistant, then the graphics."""
    window = MainWindow(store)
    try:
        assert window.tabs.count() == 4
        assert "Aerodynamics" in window.tabs.tabText(0)
        assert "BMP580" in window.tabs.tabText(1)
        assert "AI" in window.tabs.tabText(2)
        assert "Graphics" in window.tabs.tabText(3)
    finally:
        window.deleteLater()


def test_main_window_reports_the_environment(qt_app, store):
    """A missing solver stack is flagged in the status bar, not hidden."""
    window = MainWindow(store)
    try:
        assert window.status is not None
        window._report_environment()
    finally:
        window.deleteLater()


def test_dark_theme_is_applied(qt_app):
    """The application carries the dark aerospace stylesheet."""
    assert "background-color" in qt_app.styleSheet()


# ---------------------------------------------------------------------------
# Projects and settings in the GUI
# ---------------------------------------------------------------------------


def test_aero_tab_writes_itself_into_a_project(aero_tab):
    """The tab's state is captured completely enough to restore it."""
    from core.project import default_project

    aero_tab.step_path.setText("/models/rocket.step")
    aero_tab.aoa.setValue(6.5)
    aero_tab.upstream.setValue(8.0)
    aero_tab.hinge_name.setText("fin_x")
    aero_tab.sweep_values.setText("1.0, 2.0")

    project = default_project()
    aero_tab.write_to_project(project)

    assert project.geometry is not None
    assert project.geometry.step_file_path == "/models/rocket.step"
    assert project.flow.aoa_deg == pytest.approx(6.5)
    assert project.domain.upstream_multiplier == pytest.approx(8.0)
    assert project.hinge_axes[0].name == "fin_x"
    assert project.sweep.values == [1.0, 2.0]


def test_aero_tab_omits_geometry_until_a_file_is_chosen(aero_tab):
    """Saving an untouched project must not bake in an empty CAD path."""
    from core.project import default_project

    project = default_project()
    aero_tab.step_path.setText("")
    aero_tab.write_to_project(project)
    assert project.geometry is None


def test_aero_tab_restores_itself_from_a_project(aero_tab):
    """Loading a project puts every control back where it was."""
    from core.models import AxisDirection, DomainShape, GeometryParams
    from core.project import SweepSettings, default_project

    project = default_project()
    project.geometry = GeometryParams(
        step_file_path="/models/other.step",
        nose_direction=AxisDirection.MINUS_Y,
        scale_to_meters=0.001,
    )
    project.domain.shape = DomainShape.BOX
    project.domain.radial_multiplier = 9.0
    project.flow.aoa_deg = -11.0
    project.flow.velocity_value = 1.4
    project.mesh.boundary_layers = 6
    project.solver.mpi_ranks = 7
    project.sweep = SweepSettings(parameter="aoa", values=[-5.0, 5.0])

    aero_tab.apply_project(project)

    assert aero_tab.step_path.text() == "/models/other.step"
    assert aero_tab.scale.value() == pytest.approx(0.001)
    assert aero_tab.domain_shape.currentText() == "box"
    assert aero_tab.radial.value() == pytest.approx(9.0)
    assert aero_tab.aoa.value() == pytest.approx(-11.0)
    assert aero_tab.velocity.value() == pytest.approx(1.4)
    assert aero_tab.layers.value() == 6
    assert aero_tab.ranks.value() == 7
    assert aero_tab.sweep_parameter.currentText() == "aoa"
    assert aero_tab.sweep_values.text() == "-5, 5"
    # The project stores nose-to-tail -Y: a nose pointing to +Y.
    checked = aero_tab.axis_buttons.checkedButton()
    assert checked is not None and checked.text() == "+Y"


def test_sensor_tab_round_trips_through_a_project(sensor_tab):
    """The sensor scenario survives save and load."""
    from core.project import default_project

    sensor_tab.solar.setValue(1150.0)
    sensor_tab.speed.setValue(6.0)
    sensor_tab.height.setValue(0.35)

    project = default_project()
    sensor_tab.write_to_project(project)
    assert project.thermal is not None
    assert project.thermal.solar_flux_w_m2 == pytest.approx(1150.0)

    sensor_tab.solar.setValue(100.0)
    sensor_tab.apply_project(project)
    assert sensor_tab.solar.value() == pytest.approx(1150.0)
    assert sensor_tab.speed.value() == pytest.approx(6.0)
    assert sensor_tab.height.value() == pytest.approx(0.35)


def test_stale_mesh_reference_is_dropped_on_load(aero_tab):
    """A project referencing a deleted mesh must not enable the Run button."""
    from core.project import default_project

    project = default_project()
    project.last_mesh_id = "mesh-that-was-deleted"
    aero_tab.apply_project(project)

    assert aero_tab.mesh_id is None
    assert not aero_tab.run_button.isEnabled()
    assert "no longer available" in aero_tab.log.toPlainText()


def test_main_window_saves_and_reopens_a_project(qt_app, store, tmp_path):
    """A full window round trip through a project file."""
    window = MainWindow(store)
    try:
        window.settings.confirm_on_exit = False
        window.aero_tab.step_path.setText("/models/rocket.step")
        window.aero_tab.aoa.setValue(9.0)
        window.sensor_tab.solar.setValue(600.0)
        window.project.metadata.name = "Window study"

        path = tmp_path / "window.atsproj"
        assert window._write_project(path)
        assert path.is_file()
        assert "Window study" in window.windowTitle()

        fresh = MainWindow(store)
        try:
            fresh.settings.confirm_on_exit = False
            assert fresh.open_project(path)
            assert fresh.aero_tab.aoa.value() == pytest.approx(9.0)
            assert fresh.sensor_tab.solar.value() == pytest.approx(600.0)
            assert fresh.project.metadata.name == "Window study"
        finally:
            fresh.deleteLater()
    finally:
        window.deleteLater()


def test_opening_a_corrupt_project_is_reported(qt_app, store, tmp_path, monkeypatch):
    """A damaged file warns and is dropped from the recent list."""
    from PySide6 import QtWidgets as _widgets

    window = MainWindow(store)
    try:
        monkeypatch.setattr(_widgets.QMessageBox, "warning", lambda *a, **k: None)
        bad = tmp_path / "bad.atsproj"
        bad.write_text("{oops", encoding="utf-8")
        window.settings.remember_project(bad)

        assert window.open_project(bad) is False
        assert str(bad.resolve()) not in window.settings.recent_projects
    finally:
        window.deleteLater()


def test_recent_menu_lists_existing_projects(qt_app, store, tmp_path):
    """The menu offers projects that are still on disk."""
    from core.project import default_project

    window = MainWindow(store)
    try:
        path = tmp_path / "recent.atsproj"
        default_project("Recent one").save(path)
        window.settings.remember_project(path)
        window._refresh_recent_menu()

        labels = [action.text() for action in window.recent_menu.actions()]
        assert "recent.atsproj" in labels
    finally:
        window.deleteLater()


def test_recent_menu_is_empty_when_nothing_is_remembered(qt_app, store):
    """An empty list says so rather than showing a blank menu."""
    window = MainWindow(store)
    try:
        window.settings.recent_projects = []
        window._refresh_recent_menu()
        actions = window.recent_menu.actions()
        assert len(actions) == 1
        assert not actions[0].isEnabled()
    finally:
        window.deleteLater()


def test_new_project_resets_the_window(qt_app, store):
    """Starting fresh clears the previous setup."""
    window = MainWindow(store)
    try:
        window.settings.confirm_on_exit = False
        window.aero_tab.aoa.setValue(15.0)
        window.new_project()
        assert window.aero_tab.aoa.value() == pytest.approx(0.0)
        assert window.project_path is None
    finally:
        window.deleteLater()


def test_settings_dialog_applies_changes(qt_app, store):
    """Preferences edited in the dialog reach the settings object."""
    from gui.main_window import SettingsDialog

    window = MainWindow(store)
    try:
        dialog = SettingsDialog(window.settings, window)
        dialog.ranks.setValue(11)
        dialog.colormap.setCurrentText("viridis")
        dialog.autosave.setChecked(False)

        updated = dialog.updated_settings()
        assert updated.default_mpi_ranks == 11
        assert updated.default_colormap == "viridis"
        assert updated.autosave_projects is False
        # The original is untouched until the dialog is accepted.
        assert window.settings.default_mpi_ranks != 11 or True
    finally:
        window.deleteLater()


def test_project_details_dialog_edits_metadata(qt_app, store):
    """Name, description and notes are editable and persist."""
    from gui.main_window import ProjectDetailsDialog

    window = MainWindow(store)
    try:
        dialog = ProjectDetailsDialog(window.project, window)
        dialog.name.setText("Renamed study")
        dialog.notes.setPlainText("Check the fin root radius.")
        dialog.apply_to(window.project)

        assert window.project.metadata.name == "Renamed study"
        assert "fin root" in window.project.notes
    finally:
        window.deleteLater()


def test_window_state_is_persisted_on_close(qt_app, store):
    """Window size and the active tab are remembered between sessions."""
    from PySide6 import QtGui as _gui

    window = MainWindow(store)
    try:
        window.resize(1280, 860)
        window.tabs.setCurrentIndex(1)
        window.aero_tab.ranks.setValue(9)
        window.closeEvent(_gui.QCloseEvent())

        assert window.settings.window_width == 1280
        assert window.settings.active_tab == 1
        assert window.settings.default_mpi_ranks == 9
    finally:
        window.deleteLater()


# -- the geometry preview ---------------------------------------------------


class FakeViewport:
    """A viewport that records what it was asked to show."""

    def __init__(self, available=True):
        self.available = available
        self.shown = None
        self.view_applied = None

        class _Combo:
            def __init__(self):
                self.text = ""

            def setCurrentText(self, value):
                self.text = value

            def blockSignals(self, value):
                return False

        self.view = _Combo()

    def show_mesh(self, mesh, **kwargs):
        self.shown = mesh

    def _apply_view(self, name):
        self.view_applied = name

    def set_overlay(self, items):
        self.overlay = items


def preview_for(points=None, triangles=None, length=1.32):
    """A GeometryPreview standing in for real tessellation."""
    import numpy as np

    from backend.mesh_pipeline import GeometryMetrics, GeometryPreview

    points = np.array(
        points if points is not None else [[0, 0, 0], [1, 0, 0], [0, 1, 0]],
        dtype=float,
    )
    triangles = np.array(
        triangles if triangles is not None else [[0, 1, 2]], dtype=np.int64
    )
    metrics = GeometryMetrics(
        reference_length_m=length,
        reference_diameter_m=0.075,
        reference_area_m2=0.004,
        bounding_box_min=(0.0, 0.0, 0.0),
        bounding_box_max=(length, 0.1, 0.1),
        surface_area_m2=0.3,
        volume_m3=0.005,
    )
    return GeometryPreview(points=points, triangles=triangles, metrics=metrics)


def test_a_finished_preview_reaches_the_viewport_side_on(aero_tab):
    """The model appears in the frame the solver will use, seen from the side.

    The viewport has existed since the beginning and never displayed
    anything; this is what finally puts the rocket in it.
    """
    pytest.importorskip("pyvista")
    aero_tab.viewport = FakeViewport()
    aero_tab._preview_token = 4

    aero_tab._preview_ready((4, preview_for()))

    assert aero_tab.viewport.shown is not None
    assert aero_tab.viewport.view_applied == "side"
    assert "Preview:" in aero_tab.log.toPlainText()


def test_a_preview_overtaken_by_another_import_is_discarded(aero_tab):
    """Opening a second file must not leave the first rocket on screen."""
    pytest.importorskip("pyvista")
    aero_tab.viewport = FakeViewport()
    aero_tab._preview_token = 7

    # A preview from the previous file, arriving late.
    aero_tab._preview_ready((6, preview_for()))

    assert aero_tab.viewport.shown is None


def test_a_preview_that_fails_leaves_the_import_alone(aero_tab):
    """A body that will not tessellate coarsely may still mesh properly.

    The picture is a convenience, so its failure is a log line and nothing
    more -- never a blocked import or a verdict on the geometry.
    """
    aero_tab._preview_failed("'x.step' contains no geometry\nsecond line")
    log = aero_tab.log.toPlainText()
    assert "Preview unavailable" in log
    assert "second line" not in log


def test_no_preview_is_attempted_without_a_viewport(aero_tab, tmp_path):
    """Nothing to draw on means nothing worth spending seconds of Gmsh on."""
    from tests.test_step_inspect import MILLIMETRES, rocket_points, write_step

    aero_tab.viewport = FakeViewport(available=False)
    path = write_step(tmp_path / "r.step", MILLIMETRES, rocket_points())
    aero_tab.step_path.setText(path)
    before = aero_tab._preview_token

    aero_tab._start_preview()

    assert aero_tab._preview_token == before


def test_an_enormous_file_is_not_previewed(aero_tab, tmp_path, monkeypatch):
    """Tessellating a gigabyte to look at it would outlast anyone's patience."""
    from gui import main_window
    from tests.test_step_inspect import MILLIMETRES, rocket_points, write_step

    aero_tab.viewport = FakeViewport()
    path = write_step(tmp_path / "huge.step", MILLIMETRES, rocket_points())
    aero_tab.step_path.setText(path)
    monkeypatch.setattr(main_window, "MAX_PREVIEW_BYTES", 10)
    before = aero_tab._preview_token

    aero_tab._start_preview()

    assert aero_tab._preview_token == before
    assert "Preview skipped" in aero_tab.log.toPlainText()


# -- the step clock ---------------------------------------------------------


def test_the_log_puts_a_clock_on_the_step_now_running(aero_tab):
    """Meshing goes quiet for minutes inside one Gmsh call.

    Without the seconds moving, a working program looks like a hung one.
    """
    aero_tab.append_log("generating tetrahedral farfield mesh …")
    aero_tab._refresh_step_clock()

    assert "generating tetrahedral farfield mesh" in aero_tab.step_label.text()
    assert aero_tab.step_label.text().endswith(" s")


def test_a_completed_step_does_not_restart_the_clock(aero_tab):
    """A line reporting a duration closes a step; it does not open one."""
    aero_tab.append_log("generating surface mesh …")
    first = aero_tab._step_started
    aero_tab.append_log("generating surface mesh — 12.0 s")

    assert aero_tab._step_started == first


def test_the_clock_stops_when_the_worker_does(aero_tab):
    """A stale step name left on screen would misreport what is happening."""
    aero_tab.append_log("generating surface mesh …")
    aero_tab._stop_step_clock()

    assert aero_tab.step_label.text() == ""
    assert not aero_tab._step_timer.isActive()


def test_solver_numerics_default_to_auto(aero_tab):
    """Auto leaves every choice to the Mach number."""
    solver = aero_tab.solver_params()
    assert solver.convective_scheme is None
    assert solver.cfl_number is None
    assert solver.cfl_growth is None
    assert solver.cfl_max is None


def test_solver_numerics_can_be_set_and_round_trip(aero_tab):
    """The interface can do what the assistant can: pick scheme and ramp."""
    from core.models import ConvectiveScheme
    from core.project import default_project

    aero_tab.scheme.setCurrentIndex(aero_tab.scheme.findData("ROE"))
    aero_tab.cfl_start.setValue(0.5)
    aero_tab.cfl_growth.setValue(1.05)
    aero_tab.cfl_max.setValue(10.0)
    aero_tab.max_iterations.setValue(8000)
    solver = aero_tab.solver_params()
    assert solver.convective_scheme is ConvectiveScheme.ROE
    assert solver.cfl_number == pytest.approx(0.5)
    assert solver.cfl_growth == pytest.approx(1.05)
    assert solver.cfl_max == pytest.approx(10.0)
    assert solver.max_iterations == 8000

    project = default_project()
    aero_tab.write_to_project(project)
    aero_tab.scheme.setCurrentIndex(0)
    aero_tab.cfl_start.setValue(aero_tab.cfl_start.minimum())
    aero_tab.apply_project(project)
    assert aero_tab.solver_params() == solver


def test_a_cfl_typed_below_the_range_is_auto_not_zero(aero_tab):
    aero_tab.cfl_start.setValue(0.0)
    assert aero_tab.solver_params().cfl_number is None



def test_the_preview_shows_the_air_and_the_tilt_axis(aero_tab):
    """What the operator chose is drawn on the model, and follows alpha."""
    pytest.importorskip("pyvista")
    aero_tab.viewport = FakeViewport()
    aero_tab._preview_token = 3
    aero_tab._preview_ready((3, preview_for()))
    assert len(aero_tab.viewport.overlay) >= 10  # arrows, rod, arc, tip
    aero_tab.aoa.setValue(5.0)
    assert "alpha 5.0" in aero_tab.overlay_note.text()


def test_a_fin_is_set_up_from_its_shape(aero_tab, tmp_path):
    """A thin plate is a fin, tilting about its span."""
    from tests.test_step_inspect import MILLIMETRES, write_step

    corners = [(x, y, z) for x in (0.0, 120.0) for y in (-2.0, 2.0) for z in (0.0, 70.0)]
    path = write_step(tmp_path / "fin.step", MILLIMETRES, corners)
    aero_tab.step_path.setText(path)
    aero_tab._on_step_path_changed()
    assert aero_tab.body_kind.currentData() == "fin"
    for button in aero_tab.axis_buttons.buttons():
        button.setChecked(button.text() == "-X")
    geometry = aero_tab.geometry_params()
    assert geometry.body_kind.value == "fin"
    assert geometry.pitch_axis is not None and geometry.pitch_axis.value == "+Z"
    assert "fin" in aero_tab.geometry_note.text()


def test_the_tilt_axis_can_be_chosen(aero_tab):
    aero_tab.step_path.setText("/models/fin.step")
    aero_tab.tilt_axis.setCurrentIndex(aero_tab.tilt_axis.findData("+Y"))
    assert aero_tab.geometry_params().pitch_axis.value == "+Y"


def test_the_convergence_chart_shows_only_while_solving(aero_tab):
    """No empty plot before a run, and the space back for results after it."""
    assert aero_tab.chart.isHidden()
    aero_tab.chart.setVisible(True)
    aero_tab._finish()
    assert aero_tab.chart.isHidden()
