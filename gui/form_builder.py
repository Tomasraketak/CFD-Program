"""Widgets generated from the typed parameter models.

This is the mechanism that keeps the GUI and the MCP server in step. Both read
:mod:`core.models`; the MCP server turns a model into a JSON schema, and this
module turns the same model into a form. A field gains a bound, a unit or a
description in one place and both surfaces pick it up, so "every setting is
controllable from the GUI and from an agent" stays true without anyone
maintaining two lists.

Numeric bounds become spinbox ranges, enums become combo boxes, booleans
become checkboxes, and every field's description becomes its tooltip.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, get_args, get_origin

from PySide6 import QtCore, QtWidgets
from pydantic import BaseModel
from pydantic.fields import FieldInfo

# Decimal places shown for floating-point fields.
FLOAT_DECIMALS = 4

# Spinbox range used when a field declares no bound.
UNBOUNDED = 1.0e9


@dataclass
class FieldWidget:
    """A widget bound to one model field."""

    name: str
    label: str
    widget: QtWidgets.QWidget
    getter: Callable[[], Any]
    setter: Callable[[Any], None]
    description: str = ""

    def value(self) -> Any:
        """Current value, in the type the model expects."""
        return self.getter()

    def set_value(self, value: Any) -> None:
        """Write a value into the widget."""
        self.setter(value)


def humanise(name: str) -> str:
    """Turn a field name into a readable label.

    Unit suffixes baked into field names are surfaced in brackets, so
    ``vehicle_speed_ms`` reads as "Vehicle Speed [m/s]".
    """
    units = {
        "_m2": " [m^2]",
        "_ms": " [m/s]",
        "_deg": " [deg]",
        "_pa": " [Pa]",
        "_k": " [K]",
        "_c": " [C]",
        "_m": " [m]",
        "_w": " [W]",
        "_w_m2": " [W/m^2]",
        "_w_mk": " [W/(m K)]",
        "_kg_m3": " [kg/m^3]",
        "_j_kgk": " [J/(kg K)]",
        "_nm": " [N m]",
        "_s": " [s]",
    }
    suffix = ""
    stem = name
    for token, unit in sorted(units.items(), key=lambda item: -len(item[0])):
        if name.endswith(token):
            suffix = unit
            stem = name[: -len(token)]
            break
    return stem.replace("_", " ").strip().title() + suffix


def _numeric_bounds(field: FieldInfo) -> tuple[float | None, float | None]:
    """Extract (minimum, maximum) from a field's validation metadata."""
    minimum: float | None = None
    maximum: float | None = None
    for item in field.metadata:
        for attribute, setter in (
            ("ge", "min"), ("gt", "min"), ("le", "max"), ("lt", "max")
        ):
            value = getattr(item, attribute, None)
            if value is None:
                continue
            if setter == "min":
                minimum = float(value) if minimum is None else max(minimum, float(value))
            else:
                maximum = float(value) if maximum is None else min(maximum, float(value))
    return minimum, maximum


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """Strip ``| None`` from an annotation, reporting whether it was present."""
    origin = get_origin(annotation)
    if origin is None:
        return annotation, False
    args = get_args(annotation)
    non_none = [arg for arg in args if arg is not type(None)]
    if len(non_none) == 1 and len(non_none) < len(args):
        return non_none[0], True
    return annotation, False


def build_field_widget(
    name: str, field: FieldInfo, value: Any = None
) -> FieldWidget | None:
    """Create a widget for one model field.

    Returns ``None`` for fields with no sensible single-widget
    representation, such as free-form lists, which the caller handles itself.
    """
    annotation, optional = _unwrap_optional(field.annotation)
    description = field.description or ""
    label = humanise(name)
    minimum, maximum = _numeric_bounds(field)

    if isinstance(annotation, type) and issubclass(annotation, Enum):
        combo = QtWidgets.QComboBox()
        for member in annotation:
            combo.addItem(str(member.value), member)
        if value is not None:
            index = combo.findData(value)
            if index < 0:
                index = combo.findText(str(getattr(value, "value", value)))
            if index >= 0:
                combo.setCurrentIndex(index)
        return FieldWidget(
            name, label, combo,
            getter=lambda: combo.currentData(),
            setter=lambda v: combo.setCurrentIndex(max(0, combo.findText(str(getattr(v, "value", v))))),
            description=description,
        )

    if annotation is bool:
        check = QtWidgets.QCheckBox()
        check.setChecked(bool(value))
        return FieldWidget(
            name, label, check,
            getter=check.isChecked,
            setter=lambda v: check.setChecked(bool(v)),
            description=description,
        )

    if annotation is int:
        spin = QtWidgets.QSpinBox()
        spin.setRange(
            int(minimum) if minimum is not None else -2_000_000_000,
            int(maximum) if maximum is not None else 2_000_000_000,
        )
        if value is not None:
            spin.setValue(int(value))
        return FieldWidget(
            name, label, spin,
            getter=spin.value,
            setter=lambda v: spin.setValue(int(v)),
            description=description,
        )

    if annotation is float:
        spin = QtWidgets.QDoubleSpinBox()
        spin.setDecimals(FLOAT_DECIMALS)
        spin.setRange(
            minimum if minimum is not None else -UNBOUNDED,
            maximum if maximum is not None else UNBOUNDED,
        )
        spin.setSingleStep(_step_for(minimum, maximum))
        if value is not None and not (
            isinstance(value, float) and math.isnan(value)
        ):
            spin.setValue(float(value))
        if optional:
            spin.setSpecialValueText("(auto)")
        return FieldWidget(
            name, label, spin,
            getter=spin.value,
            setter=lambda v: spin.setValue(float(v)),
            description=description,
        )

    if annotation is str:
        line = QtWidgets.QLineEdit()
        if value is not None:
            line.setText(str(value))
        return FieldWidget(
            name, label, line,
            getter=line.text,
            setter=lambda v: line.setText(str(v)),
            description=description,
        )

    return None


def _step_for(minimum: float | None, maximum: float | None) -> float:
    """A single-step size proportionate to the field's range."""
    if minimum is None or maximum is None:
        return 0.1
    span = abs(maximum - minimum)
    if span <= 0.0:
        return 0.1
    if span <= 1.0:
        return 0.01
    if span <= 100.0:
        return 0.1
    return 1.0


class ModelForm(QtWidgets.QWidget):
    """A form generated from a Pydantic model.

    Parameters
    ----------
    model_type:
        The model class to build a form for.
    initial:
        An instance whose values pre-populate the form.
    skip:
        Field names to omit, for values the surrounding UI handles itself.
    """

    valueChanged = QtCore.Signal()

    def __init__(
        self,
        model_type: type[BaseModel],
        initial: BaseModel | None = None,
        skip: tuple[str, ...] = (),
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.model_type = model_type
        self.fields: dict[str, FieldWidget] = {}

        layout = QtWidgets.QFormLayout(self)
        layout.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        layout.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )

        values = initial.model_dump() if initial is not None else {}
        for name, field in model_type.model_fields.items():
            if name in skip:
                continue
            current = getattr(initial, name, None) if initial is not None else None
            if current is None:
                current = values.get(name)
            widget = build_field_widget(name, field, current)
            if widget is None:
                continue

            if widget.description:
                widget.widget.setToolTip(widget.description)
            self.fields[name] = widget
            layout.addRow(widget.label, widget.widget)
            _connect_change(widget.widget, self.valueChanged.emit)

    def values(self) -> dict[str, Any]:
        """Current form contents as a plain dictionary."""
        return {name: widget.value() for name, widget in self.fields.items()}

    def build(self, **extra: Any) -> BaseModel:
        """Construct a validated model from the form.

        Raises
        ------
        pydantic.ValidationError
            If the combination of values is invalid. Bounds are already
            enforced by the widgets, so this catches cross-field rules.
        """
        return self.model_type(**{**self.values(), **extra})

    def set_values(self, values: dict[str, Any]) -> None:
        """Populate the form from a dictionary."""
        for name, value in values.items():
            widget = self.fields.get(name)
            if widget is not None and value is not None:
                widget.set_value(value)


def _connect_change(widget: QtWidgets.QWidget, slot: Callable[[], None]) -> None:
    """Wire a widget's change signal to a callback."""
    for signal_name in (
        "valueChanged", "currentIndexChanged", "toggled", "textChanged"
    ):
        signal = getattr(widget, signal_name, None)
        if signal is not None:
            signal.connect(lambda *_: slot())
            return


class LabelledSlider(QtWidgets.QWidget):
    """A slider with a numeric readout, for continuous parameters.

    Qt sliders are integer-only, so the value is scaled internally; the
    readout always shows the true physical value.
    """

    valueChanged = QtCore.Signal(float)

    def __init__(
        self,
        minimum: float,
        maximum: float,
        value: float,
        decimals: int = 2,
        suffix: str = "",
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._scale = 10**decimals
        self._suffix = suffix
        self._decimals = decimals

        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setRange(
            int(minimum * self._scale), int(maximum * self._scale)
        )
        self.slider.setValue(int(value * self._scale))

        self.readout = QtWidgets.QLabel()
        self.readout.setMinimumWidth(78)
        self.readout.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.readout)

        self.slider.valueChanged.connect(self._on_change)
        self._update_readout()

    def _on_change(self, _: int) -> None:
        """Refresh the readout and re-emit the physical value."""
        self._update_readout()
        self.valueChanged.emit(self.value())

    def _update_readout(self) -> None:
        """Show the current value with its unit."""
        self.readout.setText(f"{self.value():.{self._decimals}f}{self._suffix}")

    def value(self) -> float:
        """Current value in physical units."""
        return self.slider.value() / self._scale

    def setValue(self, value: float) -> None:  # noqa: N802 - Qt naming
        """Set the value in physical units."""
        self.slider.setValue(int(value * self._scale))
