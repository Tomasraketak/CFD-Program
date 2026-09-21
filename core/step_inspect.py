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


def largest_extent(path: Path | str) -> float | None:
    """Longest side of the model's bounding box, in the file's own units.

    Read from the STEP text rather than a CAD kernel, because this is used
    to sanity-check the unit *before* deciding how to import. Control points
    of a spline can sit slightly outside the true surface, which is
    immaterial at the order-of-magnitude precision needed here.

    Returns None when the file holds no readable points.
    """
    target = Path(path)
    try:
        raw = target.read_bytes()[:_MAX_BYTES]
    except OSError as error:
        raise StepInspectionError(f"could not read '{target.name}': {error}") from error

    text = _strip_comments_and_strings(raw.decode("utf-8", errors="replace"))
    low = [float("inf")] * 3
    high = [float("-inf")] * 3
    found = False
    for match in _CARTESIAN_POINT.finditer(text):
        try:
            point = (
                float(match.group("x")),
                float(match.group("y")),
                float(match.group("z")),
            )
        except ValueError:  # pragma: no cover - malformed literal
            continue
        found = True
        for axis in range(3):
            low[axis] = min(low[axis], point[axis])
            high[axis] = max(high[axis], point[axis])

    if not found:
        return None
    return max(high[axis] - low[axis] for axis in range(3))


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
