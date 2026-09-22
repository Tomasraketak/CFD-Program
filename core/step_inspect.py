"""Read what a STEP file says about itself, without a CAD kernel.

Only one question is asked here, and it is the one that silently ruins a
study when guessed wrong: **what length unit is this geometry drawn in?**
Nearly every CAD package exports millimetres, the solver works in metres,
and nothing downstream can tell the difference — a 1.3 m rocket imported as
1300 m meshes without complaint, and returns a Reynolds number three orders
of magnitude out with no error anywhere.

STEP records the answer explicitly (ISO 10303-41), so it does not have to be
guessed. The file is plain text and the unit section is a few hundred bytes,
so reading it costs nothing next to the import itself. Parsing here is
deliberately narrow: entity headers and unit declarations only, never
geometry. Anything unexpected returns None and the caller falls back to
asking the operator.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np

# Decimal prefixes STEP may attach to a base SI unit, as multipliers.
_SI_PREFIXES: dict[str, float] = {
    "EXA": 1e18,
    "PETA": 1e15,
    "TERA": 1e12,
    "GIGA": 1e9,
    "MEGA": 1e6,
    "KILO": 1e3,
    "HECTO": 1e2,
    "DECA": 1e1,
    "DECI": 1e-1,
    "CENTI": 1e-2,
    "MILLI": 1e-3,
    "MICRO": 1e-6,
    "NANO": 1e-9,
    "PICO": 1e-12,
    "FEMTO": 1e-15,
    "ATTO": 1e-18,
}

# Named non-SI lengths, used when the conversion factor cannot be followed.
_NAMED_LENGTHS: dict[str, float] = {
    "INCH": 0.0254,
    "INCHES": 0.0254,
    "FOOT": 0.3048,
    "FEET": 0.3048,
    "YARD": 0.9144,
    "MILE": 1609.344,
    "MILLIMETRE": 1e-3,
    "MILLIMETER": 1e-3,
    "CENTIMETRE": 1e-2,
    "CENTIMETER": 1e-2,
    "METRE": 1.0,
    "METER": 1.0,
    "KILOMETRE": 1e3,
    "KILOMETER": 1e3,
}

# How much of the file to read. The unit section sits in the last few
# hundred lines of a typical export, but a small file may be entirely
# geometry; reading it whole is still cheap next to the CAD import.
_MAX_BYTES = 64 * 1024 * 1024

_ENTITY = re.compile(r"#(\d+)\s*=\s*", re.ASCII)
_SI_UNIT = re.compile(
    r"SI_UNIT\s*\(\s*(?:\.(?P<prefix>[A-Z]+)\.|\$)\s*,\s*\.(?P<name>[A-Z]+)\.\s*\)",
    re.ASCII,
)
_CONVERSION = re.compile(
    r"CONVERSION_BASED_UNIT\s*\(\s*'(?P<name>[^']*)'\s*,\s*#(?P<ref>\d+)\s*\)",
    re.ASCII,
)
_MEASURE_WITH_UNIT = re.compile(
    r"LENGTH_MEASURE\s*\(\s*(?P<value>[-+0-9.eE]+)\s*\)\s*,\s*#(?P<ref>\d+)",
    re.ASCII,
)
_GLOBAL_UNITS = re.compile(
    r"GLOBAL_UNIT_ASSIGNED_CONTEXT\s*\(\s*\((?P<refs>[^)]*)\)\s*\)", re.ASCII
)
_REFERENCE = re.compile(r"#(\d+)", re.ASCII)


class StepInspectionError(RuntimeError):
    """Raised when the file cannot be read at all."""


def _strip_comments_and_strings(text: str) -> str:
    """Blank out STEP comments and string bodies.

    Entity separators only count outside strings and comments, and a file
    name like ``'a;b.step'`` in the header would otherwise split an entity
    in the wrong place. Blanking rather than deleting keeps every character
    offset intact, so the result can be scanned with plain regexes.
    """
    out = list(text)
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "'":
            index += 1
            while index < length:
                if text[index] == "'":
                    # A doubled quote is an escaped quote, not the end.
                    if index + 1 < length and text[index + 1] == "'":
                        out[index] = out[index + 1] = " "
                        index += 2
                        continue
                    break
                out[index] = " "
                index += 1
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            end = text.find("*/", index + 2)
            end = length if end == -1 else end + 2
            for position in range(index, end):
                out[position] = " "
            index = end
            continue
        index += 1
    return "".join(out)


def _entities(text: str) -> dict[int, str]:
    """Map entity id to its body, for a cleaned STEP data section."""
    entities: dict[int, str] = {}
    for match in _ENTITY.finditer(text):
        end = text.find(";", match.end())
        if end == -1:
            continue
        entities[int(match.group(1))] = text[match.end() : end]
    return entities


def _si_length_factor(body: str) -> float | None:
    """Metres per unit for an SI length declaration, else None."""
    match = _SI_UNIT.search(body)
    if match is None or match.group("name") not in ("METRE", "METER"):
        return None
    prefix = match.group("prefix")
    if prefix is None:
        return 1.0
    return _SI_PREFIXES.get(prefix)


def _length_factor(
    identifier: int, entities: dict[int, str], seen: frozenset[int] = frozenset()
) -> float | None:
    """Metres per unit for one length-unit entity, following conversions."""
    if identifier in seen:  # A cyclic file would otherwise recurse forever.
        return None
    body = entities.get(identifier)
    if body is None:
        return None

    direct = _si_length_factor(body)
    if direct is not None:
        return direct

    conversion = _CONVERSION.search(body)
    if conversion is None:
        return None

    # Prefer the file's own conversion factor over the unit's name: a file
    # that says an inch is 25.4 mm is authoritative, a name is a guess.
    measure = entities.get(int(conversion.group("ref")), "")
    factor_match = _MEASURE_WITH_UNIT.search(measure)
    if factor_match is not None:
        base = _length_factor(
            int(factor_match.group("ref")), entities, seen | {identifier}
        )
        if base is not None:
            try:
                return float(factor_match.group("value")) * base
            except ValueError:
                pass

    return _NAMED_LENGTHS.get(conversion.group("name").strip().upper())


def detect_length_unit(path: Path | str) -> float | None:
    """Metres per CAD unit, read from the file's declared length unit.

    Returns
    -------
    float or None
        The multiplier converting the file's coordinates to metres — 0.001
        for a millimetre model. None when the file declares no length unit,
        declares several that disagree, or cannot be parsed. None means "ask
        the operator", never "assume metres".

    Raises
    ------
    StepInspectionError
        If the file cannot be read from disk at all.
    """
    target = Path(path)
    try:
        raw = target.read_bytes()[:_MAX_BYTES]
    except OSError as error:
        raise StepInspectionError(f"could not read '{target.name}': {error}") from error

    text = _strip_comments_and_strings(raw.decode("utf-8", errors="replace"))
    entities = _entities(text)
    if not entities:
        return None

    # The units that matter are the ones the geometry's representation
    # context points at. A file may also carry unreferenced leftovers — this
    # very rocket declares both millimetres and metres, and only the
    # millimetre entity is actually used.
    referenced: set[int] = set()
    for context in _GLOBAL_UNITS.finditer(text):
        referenced.update(
            int(reference) for reference in _REFERENCE.findall(context.group("refs"))
        )

    candidates = referenced or set(entities)
    factors = {
        factor
        for identifier in candidates
        if "LENGTH_UNIT" in entities.get(identifier, "")
        and (factor := _length_factor(identifier, entities)) is not None
    }

    if len(factors) != 1:
        # No declaration, or a file that contradicts itself. Either way the
        # honest answer is that it is not known.
        return None
    factor = factors.pop()
    return factor if factor > 0.0 else None


_CARTESIAN_POINT = re.compile(
    r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*"
    r"(?P<x>[-+0-9.eE]+)\s*,\s*(?P<y>[-+0-9.eE]+)\s*,\s*(?P<z>[-+0-9.eE]+)",
    re.ASCII,
)

# What this platform is for: a sounding rocket a few metres long, or a
# sensor enclosure a few centimetres across. A model whose largest dimension
# lands outside this band, once scaled, is being read in the wrong unit.
MIN_PLAUSIBLE_EXTENT_M = 0.01
MAX_PLAUSIBLE_EXTENT_M = 100.0


def read_points(path: Path | str) -> np.ndarray:
    """Every Cartesian point in the file, as an ``(n, 3)`` array.

    Read from the STEP text rather than a CAD kernel, because these answers
    are needed *before* deciding how to import. Control points of a spline
    can sit slightly outside the true surface, which is immaterial at the
    precision wanted here: an order of magnitude for the unit, and which end
    of the body is thinner for the nose.
    """
    target = Path(path)
    try:
        raw = target.read_bytes()[:_MAX_BYTES]
    except OSError as error:
        raise StepInspectionError(f"could not read '{target.name}': {error}") from error

    text = _strip_comments_and_strings(raw.decode("utf-8", errors="replace"))
    values: list[float] = []
    for match in _CARTESIAN_POINT.finditer(text):
        try:
            values.extend(
                (
                    float(match.group("x")),
                    float(match.group("y")),
                    float(match.group("z")),
                )
            )
        except ValueError:  # pragma: no cover - malformed literal
            continue
    if not values:
        return np.empty((0, 3), dtype=float)
    return np.asarray(values, dtype=float).reshape(-1, 3)


def largest_extent(path: Path | str) -> float | None:
    """Longest side of the model's bounding box, in the file's own units.

    Returns None when the file holds no readable points.
    """
    points = read_points(path)
    if points.size == 0:
        return None
    return float((points.max(axis=0) - points.min(axis=0)).max())


class ScaleDecision:
    """How a file's CAD units were resolved, and why.

    Carrying the reasoning alongside the number is the point: a scale chosen
    by inference must be reported to whoever is about to spend twenty
    minutes of solver time on it.
    """

    __slots__ = ("scale", "reason", "confident", "declared", "extent")

    def __init__(
        self,
        scale: float,
        reason: str,
        confident: bool,
        declared: float | None,
        extent: float | None,
    ) -> None:
        self.scale = scale
        self.reason = reason
        self.confident = confident
        self.declared = declared
        self.extent = extent

    @property
    def extent_m(self) -> float | None:
        """Largest dimension in metres once the scale is applied."""
        return None if self.extent is None else self.extent * self.scale

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return (
            f"ScaleDecision(scale={self.scale!r}, confident={self.confident!r}, "
            f"reason={self.reason!r})"
        )


def suggest_scale_to_meters(path: Path | str) -> ScaleDecision:
    """Decide what multiplier turns this file's coordinates into metres.

    The declared unit is believed first, then checked against the model's
    actual size, because exporters do lie: OpenCASCADE writes a millimetre
    header onto a model whose coordinates are plainly metres, and following
    that header blindly would shrink a 1 m rocket to 1 mm. A declaration
    that survives the size check is used; one that does not is reported and
    overruled.
    """
    declared = detect_length_unit(path)
    extent = largest_extent(path)

    def plausible(scale: float) -> bool:
        if extent is None or extent <= 0.0:
            return False
        return MIN_PLAUSIBLE_EXTENT_M <= extent * scale <= MAX_PLAUSIBLE_EXTENT_M

    if declared is not None and plausible(declared):
        return ScaleDecision(
            declared,
            f"the file declares {describe_length_unit(declared)}",
            True,
            declared,
            extent,
        )

    if declared is not None and plausible(1.0):
        return ScaleDecision(
            1.0,
            f"the file declares {describe_length_unit(declared)}, but that "
            f"would make the model {extent * declared:.4g} m across; its "
            "coordinates are already metres",
            True,
            declared,
            extent,
        )

    if declared is not None:
        return ScaleDecision(
            declared,
            f"the file declares {describe_length_unit(declared)}; the model "
            "is an unusual size either way, so check this",
            False,
            declared,
            extent,
        )

    if plausible(1.0):
        return ScaleDecision(
            1.0,
            "no unit is declared; the coordinates are the right size for metres",
            False,
            None,
            extent,
        )

    if plausible(1e-3):
        return ScaleDecision(
            1e-3,
            "no unit is declared; the coordinates are the right size for "
            "millimetres",
            False,
            None,
            extent,
        )

    return ScaleDecision(
        1.0,
        "no unit is declared and the model size gives no hint; metres assumed",
        False,
        None,
        extent,
    )


# ---------------------------------------------------------------------------
# Which way the rocket points
# ---------------------------------------------------------------------------

# A body must be this much longer than it is wide before its long axis is
# treated as an airframe axis. Below it the model is a box or a bracket and
# "which way does the nose point" has no answer worth guessing at.
MIN_SLENDERNESS = 2.0

# Fraction of the length sampled at each end when comparing how thick the
# two ends are. A tenth is short enough to sit inside a nose cone and long
# enough to average out a sparse point cloud.
_END_FRACTION = 0.1

# How much thinner one end must be before it is called the nose. A rocket's
# nose tapers to a point while its tail carries fins and a flat base, so the
# real ratio is several times this; the margin is for bodies that barely
# taper at all.
_NOSE_RATIO = 1.25

# Fins are looked for in this fraction of the length at each end: a fin root
# chord is rarely more than a fifth of a rocket, and a nozzle can push the
# fins a little further from the very end.
_FIN_FRACTION = 0.3

# An end reaching this many times the body radius carries fins, provided it
# also reaches this much further than the other end. The comparison is
# between the ends rather than against the body because the file's points
# include spline control points, which stand off a curved nose: on one real
# rocket they reached 1.4 body radii with no fin anywhere near.
_FIN_REACH = 1.6
_FIN_END_RATIO = 1.5


class AxisDecision:
    """Which axis a slender body lies along, and where its nose is.

    Two directions are carried here and they are opposites, which is worth
    stating plainly because getting them confused meshes a rocket backwards.
    ``nose_end`` is where the tip physically is: the +Y end of a model drawn
    nose-up. ``nose_direction`` is the value the meshing parameter of that
    name takes, which is the axis the body runs along **from the nose
    towards the tail** -- ``-Y`` for that same model. The pipeline rotates
    that direction onto +X, which puts the nose at the upstream end of the
    wind tunnel, where the farfield gives it room.
    """

    __slots__ = (
        "axis",
        "nose_end",
        "nose_direction",
        "confident",
        "reason",
        "slenderness",
        "nose_radius",
        "tail_radius",
    )

    def __init__(
        self,
        axis: str,
        nose_end: str | None,
        confident: bool,
        reason: str,
        slenderness: float,
        nose_radius: float,
        tail_radius: float,
    ) -> None:
        self.axis = axis
        self.nose_end = nose_end
        self.nose_direction = _opposite(nose_end)
        self.confident = confident
        self.reason = reason
        self.slenderness = slenderness
        self.nose_radius = nose_radius
        self.tail_radius = tail_radius

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for the MCP tools."""
        return {
            "axis": self.axis,
            "nose_end": self.nose_end,
            "nose_direction": self.nose_direction,
            "confident": self.confident,
            "reason": self.reason,
            "slenderness": self.slenderness,
            "nose_end_radius": self.nose_radius,
            "tail_end_radius": self.tail_radius,
        }

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return (
            f"AxisDecision(nose_end={self.nose_end!r}, "
            f"nose_direction={self.nose_direction!r}, "
            f"confident={self.confident!r}, reason={self.reason!r})"
        )


def _opposite(direction: str | None) -> str | None:
    """Flip a signed axis name, or pass None through."""
    if not direction:
        return None
    return ("-" if direction[0] == "+" else "+") + direction[1:]


def _mean_radius(points: np.ndarray, axis: int, centre: np.ndarray) -> float:
    """Mean distance from the body axis, for a slice of the point cloud."""
    if points.size == 0:
        return 0.0
    lateral = np.delete(points, axis, axis=1) - np.delete(centre, axis)
    return float(np.linalg.norm(lateral, axis=1).mean())


def _fin_end(
    points: np.ndarray,
    axis: int,
    low: float,
    high: float,
    length: float,
    centre: np.ndarray,
) -> tuple[bool, float, float] | None:
    """Which end carries fins, if exactly one visibly does.

    Returns ``(tail_is_high_end, body_radius, fin_reach)``, or None when
    neither end, or both, stand out from the body.
    """
    lateral = np.linalg.norm(
        np.delete(points, axis, axis=1) - np.delete(centre, axis), axis=1
    )
    along = points[:, axis]
    middle = (along > low + 0.3 * length) & (along < high - 0.3 * length)
    if np.count_nonzero(middle) < 4:
        return None
    body_radius = float(np.median(lateral[middle]))
    if body_radius <= 0.0:
        return None
    window = _FIN_FRACTION * length
    low_reach = float(lateral[along <= low + window].max(initial=0.0))
    high_reach = float(lateral[along >= high - window].max(initial=0.0))
    if low_reach > _FIN_REACH * body_radius and low_reach > _FIN_END_RATIO * high_reach:
        return False, body_radius, low_reach
    if high_reach > _FIN_REACH * body_radius and high_reach > _FIN_END_RATIO * low_reach:
        return True, body_radius, high_reach
    return None


def detect_body_axis(path: Path | str) -> AxisDecision:
    """Work out which way a slender body points, from the CAD alone.

    Getting this wrong is the single most expensive mistake available here:
    a rocket meshed backwards produces a complete, plausible set of forces
    for a vehicle flying tail-first. The operator should not have to work it
    out from a coordinate system they may not have chosen.

    Two questions, answered separately because they carry different
    confidence. The **axis** is just the longest dimension, which is not
    really a guess for anything slender. The **end** is inferred from the
    shape: a nose tapers, while a tail carries fins and a blunt base, so the
    thinner end is the nose. When the two ends are alike -- a plain tube, a
    body with a boat tail -- no direction is returned and the caller is
    expected to ask rather than assume.
    """
    points = read_points(path)
    if points.shape[0] < 8:
        return AxisDecision(
            "X", None, False, "too few points in the file to tell", 0.0, 0.0, 0.0
        )

    span = points.max(axis=0) - points.min(axis=0)
    axis = int(np.argmax(span))
    name = "XYZ"[axis]
    length = float(span[axis])
    width = float(np.max(np.delete(span, axis)))
    slenderness = length / width if width > 0.0 else float("inf")

    if length <= 0.0 or slenderness < MIN_SLENDERNESS:
        return AxisDecision(
            name,
            None,
            False,
            f"the model is only {slenderness:.1f} times longer than it is "
            "wide, so it has no obvious nose",
            slenderness,
            0.0,
            0.0,
        )

    low = float(points[:, axis].min())
    high = float(points[:, axis].max())
    margin = _END_FRACTION * length
    centre = points.mean(axis=0)

    low_end = points[points[:, axis] <= low + margin]
    high_end = points[points[:, axis] >= high - margin]
    low_radius = _mean_radius(low_end, axis, centre)
    high_radius = _mean_radius(high_end, axis, centre)

    if low_radius <= 0.0 and high_radius <= 0.0:  # pragma: no cover - degenerate
        return AxisDecision(
            name, None, False, "both ends measure as points", slenderness, 0.0, 0.0
        )

    # Fins first, because they are unambiguous: they are at the tail. The
    # taper test alone can be fooled by a motor nozzle, which is thin and
    # sits at the very end of the tail -- a long enough one makes the tail
    # the thinner end.
    # The bounding-box middle, not the point mean: points bunch up wherever
    # the geometry is detailed, which drags the mean off the axis.
    axis_centre = 0.5 * (points.min(axis=0) + points.max(axis=0))
    fin_end = _fin_end(points, axis, low, high, length, axis_centre)
    if fin_end is not None:
        tail_is_high, body_radius, reach = fin_end
        nose_end = f"-{name}" if tail_is_high else f"+{name}"
        return AxisDecision(
            name,
            nose_end,
            True,
            f"the body lies along {name} and carries fins at the "
            f"{'+' if tail_is_high else '-'}{name} end, reaching "
            f"{reach / body_radius:.1f} times the body radius, so the nose is "
            f"at {nose_end}",
            slenderness,
            high_radius if tail_is_high else low_radius,
            low_radius if tail_is_high else high_radius,
        )

    # The nose is the thinner end: it tapers, the tail does not.
    if high_radius * _NOSE_RATIO < low_radius:
        nose_end, nose, tail = f"+{name}", high_radius, low_radius
    elif low_radius * _NOSE_RATIO < high_radius:
        nose_end, nose, tail = f"-{name}", low_radius, high_radius
    else:
        return AxisDecision(
            name,
            None,
            False,
            f"the body lies along {name}, but both ends are about equally "
            f"thick ({low_radius:.4g} and {high_radius:.4g}), so which one "
            "is the nose cannot be read from the shape",
            slenderness,
            min(low_radius, high_radius),
            max(low_radius, high_radius),
        )

    return AxisDecision(
        name,
        nose_end,
        True,
        f"the body lies along {name} and tapers towards {nose_end}, where "
        f"it is {tail / nose:.1f} times thinner than at the other end",
        slenderness,
        nose,
        tail,
    )


def describe_length_unit(factor: float | None) -> str:
    """A short human name for a scale factor, for status lines and logs."""
    if factor is None:
        return "not declared"
    names = {
        1.0: "metres",
        1e-3: "millimetres",
        1e-2: "centimetres",
        1e3: "kilometres",
        0.0254: "inches",
        0.3048: "feet",
    }
    for value, name in names.items():
        if abs(factor - value) <= 1e-12 * max(1.0, abs(value)):
            return name
    return f"{factor:g} m per unit"
