"""Reading a STEP file's declared units, and deciding whether to believe it.

The stakes here are quiet: a millimetre model imported as metres is a
kilometre-long rocket that meshes, solves and returns numbers, all of them
wrong by three orders of magnitude. These tests pin down both halves of the
answer -- what the file says, and whether what it says survives contact with
the model's actual size.
"""

from __future__ import annotations

import pytest

from core.step_inspect import (
    StepInspectionError,
    describe_length_unit,
    detect_length_unit,
    largest_extent,
    suggest_scale_to_meters,
)


def write_step(
    path,
    units: str,
    points: list[tuple[float, float, float]],
    refs: str = "#20,#30",
) -> str:
    """A STEP file with a unit declaration and some Cartesian points.

    Only the parts this module reads are real; there is no geometry, which
    is the point -- the reader must not need a CAD kernel.
    """
    body = [
        "ISO-10303-21;",
        "HEADER;",
        "FILE_NAME('part;name.step','2026-01-01T00:00:00',(''),(''),'','','');",
        "ENDSEC;",
        "DATA;",
        "#10=(GEOMETRIC_REPRESENTATION_CONTEXT(3)",
        f"GLOBAL_UNIT_ASSIGNED_CONTEXT(({refs}))",
        "REPRESENTATION_CONTEXT('','3D'));",
        units,
        "#30=(NAMED_UNIT(*)PLANE_ANGLE_UNIT()SI_UNIT($,.RADIAN.));",
    ]
    for index, (x, y, z) in enumerate(points):
        body.append(f"#{100 + index}=CARTESIAN_POINT('',({x!r},{y!r},{z!r}));")
    body += ["ENDSEC;", "END-ISO-10303-21;"]
    path.write_text("\n".join(body), encoding="utf-8")
    return str(path)


MILLIMETRES = "#20=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));"
METRES = "#20=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT($,.METRE.));"
CENTIMETRES = "#20=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.CENTI.,.METRE.));"


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declaration, expected",
    [(MILLIMETRES, 1e-3), (METRES, 1.0), (CENTIMETRES, 1e-2)],
)
def test_the_declared_si_unit_is_read(tmp_path, declaration, expected):
    """A plain SI length declaration is the easy, and usual, case."""
    target = write_step(tmp_path / "part.step", declaration, [(0, 0, 0)])
    assert detect_length_unit(target) == pytest.approx(expected)


def test_an_inch_file_is_converted_through_its_own_factor(tmp_path):
    """A conversion-based unit carries the factor; the name is a fallback."""
    units = (
        "#20=(CONVERSION_BASED_UNIT('INCH',#21)LENGTH_UNIT()NAMED_UNIT(#22));\n"
        "#21=LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(25.4),#22);\n"
        "#22=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));"
    )
    target = write_step(tmp_path / "part.step", units, [(0, 0, 0)])
    assert detect_length_unit(target) == pytest.approx(0.0254)


def test_an_undeclared_unit_returns_none_rather_than_a_guess(tmp_path):
    """No declaration must mean 'ask', never 'assume metres'."""
    target = write_step(tmp_path / "part.step", "#20=SOMETHING_ELSE();", [(0, 0, 0)])
    assert detect_length_unit(target) is None


def test_only_the_referenced_unit_counts(tmp_path):
    """Exporters leave unused unit entities behind; they are not the answer.

    The rocket this was written for declares both millimetres and metres,
    and only the millimetre entity is referenced by the geometry's context.
    """
    units = MILLIMETRES + "\n#40=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT($,.METRE.));"
    target = write_step(tmp_path / "part.step", units, [(0, 0, 0)])
    assert detect_length_unit(target) == pytest.approx(1e-3)


def test_contradictory_referenced_units_are_not_resolved(tmp_path):
    """Two different length units in one context is not a decision to make."""
    units = (
        "#20=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));\n"
        "#21=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT($,.METRE.));"
    )
    target = write_step(
        tmp_path / "part.step", units, [(0, 0, 0)], refs="#20,#21,#30"
    )
    assert detect_length_unit(target) is None


def test_a_semicolon_inside_a_string_does_not_split_an_entity(tmp_path):
    """STEP strings may contain the entity terminator; parsing must not care."""
    target = write_step(tmp_path / "part.step", MILLIMETRES, [(0, 0, 0)])
    assert "part;name.step" in (tmp_path / "part.step").read_text(encoding="utf-8")
    assert detect_length_unit(target) == pytest.approx(1e-3)


def test_an_unreadable_file_is_reported(tmp_path):
    """A missing file is an error, not a silent None."""
    with pytest.raises(StepInspectionError):
        detect_length_unit(tmp_path / "absent.step")


# ---------------------------------------------------------------------------
# The size
# ---------------------------------------------------------------------------


def test_the_largest_extent_comes_from_the_points(tmp_path):
    """The bounding box is read from the text, with no CAD kernel."""
    target = write_step(
        tmp_path / "part.step",
        MILLIMETRES,
        [(0.0, 0.0, 0.0), (1300.0, 90.0, -90.0)],
    )
    assert largest_extent(target) == pytest.approx(1300.0)


def test_a_file_without_points_has_no_extent(tmp_path):
    """Nothing to measure is None, not zero."""
    target = write_step(tmp_path / "part.step", MILLIMETRES, [])
    assert largest_extent(target) is None


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def test_a_millimetre_model_of_plausible_size_is_believed(tmp_path):
    """The ordinary case: a 1.32 m rocket drawn in millimetres."""
    target = write_step(
        tmp_path / "rocket.step", MILLIMETRES, [(0, 0, 0), (1320.0, 92.5, 92.5)]
    )
    decision = suggest_scale_to_meters(target)
    assert decision.scale == pytest.approx(1e-3)
    assert decision.confident
    assert decision.extent_m == pytest.approx(1.32)
    assert "millimetres" in decision.reason


def test_a_false_millimetre_header_is_overruled_by_the_model_size(tmp_path):
    """OpenCASCADE writes a millimetre header onto a metre-scale model.

    Following it would shrink a 1 m rocket to 1 mm. Our own sample geometry
    is written exactly this way, so this is not a hypothetical.
    """
    target = write_step(
        tmp_path / "sample.step", MILLIMETRES, [(0, 0, 0), (1.0, 0.09, 0.09)]
    )
    decision = suggest_scale_to_meters(target)
    assert decision.scale == pytest.approx(1.0)
    assert decision.confident
    assert decision.extent_m == pytest.approx(1.0)
    assert "already metres" in decision.reason


def test_the_real_sample_rocket_is_read_as_metres(tmp_path):
    """The end-to-end version of the case above, on a real generated file."""
    pytest.importorskip("gmsh")
    from backend.sample_geometry import create_reference_rocket_step

    target = create_reference_rocket_step(tmp_path / "reference_rocket.step")
    decision = suggest_scale_to_meters(target)
    assert decision.scale == pytest.approx(1.0)
    assert decision.extent_m == pytest.approx(1.0, rel=0.05)


def test_an_undeclared_file_is_inferred_but_not_trusted(tmp_path):
    """A size-based guess is reported as a guess."""
    target = write_step(
        tmp_path / "part.step", "#20=NOTHING();", [(0, 0, 0), (2.0, 0.1, 0.1)]
    )
    decision = suggest_scale_to_meters(target)
    assert decision.scale == pytest.approx(1.0)
    assert not decision.confident
    assert "no unit is declared" in decision.reason


def test_an_implausible_model_keeps_its_declaration_and_says_so(tmp_path):
    """A 400 m body is not what this program is for; flag it, do not fix it."""
    target = write_step(
        tmp_path / "part.step", METRES, [(0, 0, 0), (400.0, 10.0, 10.0)]
    )
    decision = suggest_scale_to_meters(target)
    assert decision.scale == pytest.approx(1.0)
    assert not decision.confident
    assert "check this" in decision.reason


@pytest.mark.parametrize(
    "factor, name",
    [(1.0, "metres"), (1e-3, "millimetres"), (0.0254, "inches"), (None, "not declared")],
)
def test_units_are_named_in_words(factor, name):
    """Status lines and assistant replies read better than '0.001'."""
    assert describe_length_unit(factor) == name
