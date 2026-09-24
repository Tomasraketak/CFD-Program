"""Mesh pipeline: orientation, classification and end-to-end generation.

The end-to-end cases run the real toolchain -- OpenCASCADE import, Gmsh
surface meshing, the prism extruder and Gmsh tetrahedralisation -- so they are
slow by unit-test standards. They use a capsule rather than the full finned
rocket to keep the suite usable, and are marked ``slow``.
"""

from __future__ import annotations

import math
from pathlib import Path

import gmsh
import numpy as np
import pytest

from backend.gmsh_session import gmsh_session
from backend.mesh_pipeline import (
    MARKER_FARFIELD,
    MARKER_WALL_FINS,
    MARKER_WALL_ROCKET,
    HealingReport,
    MeshPipelineError,
    _is_closed_face_failure,
    _split_closed_faces,
    classify_wall_surfaces,
    compact_triangulation,
    generate_mesh,
    rotation_matrix_from_vectors,
)
from backend.su2_mesh import read_su2_header
from core.models import (
    AxisDirection,
    DomainParams,
    DomainShape,
    FlowParams,
    GeometryParams,
    MeshParams,
    MeshRequest,
    MeshResolution,
    SimulationTrack,
)


# ---------------------------------------------------------------------------
# Orientation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        [1.0, 0.0, 0.0],
        [-1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, -1.0],
        [0.3, -0.5, 0.81],
        [-2.0, 7.0, -0.5],
    ],
)
def test_rotation_maps_source_onto_target(source):
    """Every direction, including the antiparallel case, rotates onto +X."""
    target = np.array([1.0, 0.0, 0.0])
    rotation = rotation_matrix_from_vectors(np.array(source), target)
    unit_source = np.array(source) / np.linalg.norm(source)
    assert rotation @ unit_source == pytest.approx(target, abs=1e-12)


@pytest.mark.parametrize(
    "source",
    [[1.0, 0.0, 0.0], [-1.0, 0.0, 0.0], [0.4, 0.2, -0.9]],
)
def test_rotation_matrices_are_orthonormal(source):
    """A rotation must preserve lengths and have unit determinant."""
    rotation = rotation_matrix_from_vectors(
        np.array(source), np.array([1.0, 0.0, 0.0])
    )
    assert rotation @ rotation.T == pytest.approx(np.eye(3), abs=1e-12)
    assert np.linalg.det(rotation) == pytest.approx(1.0)


def test_antiparallel_rotation_is_not_singular():
    """-X onto +X has no unique axis and must still produce a real rotation.

    Rodrigues' formula degenerates here because the cross product vanishes;
    without the special case the result is singular.
    """
    rotation = rotation_matrix_from_vectors(
        np.array([-1.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0])
    )
    assert np.linalg.det(rotation) == pytest.approx(1.0)
    assert rotation @ np.array([-1.0, 0.0, 0.0]) == pytest.approx([1.0, 0.0, 0.0])


def test_identity_rotation_for_aligned_vectors():
    """An already-aligned nose vector needs no rotation."""
    rotation = rotation_matrix_from_vectors(
        np.array([1.0, 0.0, 0.0]), np.array([1.0, 0.0, 0.0])
    )
    assert rotation == pytest.approx(np.eye(3))


# ---------------------------------------------------------------------------
# Wall classification
# ---------------------------------------------------------------------------


def test_classify_separates_fins_from_the_airframe():
    """Faces reaching past the airframe radius are called fins."""
    with gmsh_session("classify"):
        occ = gmsh.model.occ
        barrel = occ.addCylinder(0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.04)
        # A plate extending well beyond the barrel radius.
        fin = occ.addBox(0.6, -0.002, 0.03, 0.2, 0.004, 0.08)
        occ.fuse([(3, barrel)], [(3, fin)])
        occ.synchronize()

        volumes = [tag for _, tag in gmsh.model.getEntities(3)]
        boundary = gmsh.model.getBoundary(
            [(3, v) for v in volumes], oriented=False, combined=True
        )
        tags = sorted({abs(int(t)) for _, t in boundary})
        airframe, fins, radius = classify_wall_surfaces(tags)

    assert radius == pytest.approx(0.04, rel=0.2)
    assert fins, "the protruding plate should be classified as a fin"
    assert airframe, "the barrel should be classified as airframe"
    assert len(airframe) + len(fins) == len(tags)


def test_classify_handles_a_body_with_no_fins():
    """A plain body of revolution yields no fin faces."""
    with gmsh_session("nofins"):
        gmsh.model.occ.addCylinder(0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.05)
        gmsh.model.occ.synchronize()
        tags = [tag for _, tag in gmsh.model.getEntities(2)]
        airframe, fins, radius = classify_wall_surfaces(tags)

    assert fins == []
    assert len(airframe) == len(tags)
    assert radius == pytest.approx(0.05, rel=0.2)


def test_classify_of_nothing_is_empty():
    """An empty surface list does not raise."""
    assert classify_wall_surfaces([]) == ([], [], 0.0)


# ---------------------------------------------------------------------------
# Triangulation helpers
# ---------------------------------------------------------------------------


def test_compact_triangulation_drops_unreferenced_nodes():
    """Whole-model node arrays carry nodes the wall does not use."""
    points = np.array(
        [[0.0, 0.0, 0.0], [9.0, 9.0, 9.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    )
    triangles = np.array([[0, 2, 3]])
    compacted, reindexed, kept = compact_triangulation(points, triangles)

    assert len(compacted) == 3
    assert kept.tolist() == [0, 2, 3]
    assert np.allclose(compacted[reindexed], points[triangles])


def test_compact_triangulation_of_nothing():
    """An empty triangulation compacts to empty arrays."""
    compacted, reindexed, kept = compact_triangulation(
        np.zeros((3, 3)), np.empty((0, 3), dtype=np.int64)
    )
    assert compacted.shape == (0, 3)
    assert kept.size == 0


# ---------------------------------------------------------------------------
# End-to-end generation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def capsule_step(tmp_path_factory):
    """A capsule body: manifold, smooth, and quick to mesh."""
    path = tmp_path_factory.mktemp("cad") / "capsule.step"
    with gmsh_session("capsule"):
        occ = gmsh.model.occ
        barrel = occ.addCylinder(0.0, 0.0, 0.0, 0.5, 0.0, 0.0, 0.04)
        nose = occ.addSphere(0.0, 0.0, 0.0, 0.04)
        tail = occ.addSphere(0.5, 0.0, 0.0, 0.04)
        occ.fuse([(3, barrel)], [(3, nose), (3, tail)])
        occ.synchronize()
        gmsh.write(str(path))
    return path


def capsule_request(step_path, **overrides) -> MeshRequest:
    """A fast meshing request: few layers, no cell-count targeting."""
    mesh_kwargs = {
        "boundary_layers": 5,
        "max_targeting_iterations": 0,
        "farfield_size_multiplier": 60.0,
        **overrides.pop("mesh", {}),
    }
    return MeshRequest(
        geometry=GeometryParams(step_file_path=str(step_path), **overrides.pop("geometry", {})),
        domain=DomainParams(**overrides.pop("domain", {})),
        mesh=MeshParams(**mesh_kwargs),
        sizing_flow=FlowParams(velocity_value=2.0),
    )


@pytest.mark.slow
def test_end_to_end_mesh_generation(capsule_step, tmp_path):
    """A STEP file becomes a tagged, solver-ready .su2 mesh."""
    output = tmp_path / "capsule.su2"
    result = generate_mesh(capsule_request(capsule_step), output)

    assert output.is_file()
    assert result.cell_count > 0
    assert result.node_count > 0
    assert result.min_quality > 0.0

    # Reference dimensions recovered from the CAD: 0.58 m long, 0.08 m across.
    assert result.reference_length_m == pytest.approx(0.58, rel=0.05)
    assert result.reference_diameter_m == pytest.approx(0.08, rel=0.1)

    # The whole point of the prism stack is hitting the requested y+.
    assert result.estimated_yplus == pytest.approx(45.0, rel=1e-6)

    header = read_su2_header(output)
    assert header["cell_count"] == result.cell_count
    assert header["node_count"] == result.node_count
    assert MARKER_WALL_ROCKET in header["markers"]
    assert MARKER_FARFIELD in header["markers"]
    assert header["markers"] == result.boundary_markers


@pytest.mark.slow
def test_mesh_contains_both_prisms_and_tetrahedra(capsule_step, tmp_path):
    """The hybrid mesh must have a prism layer, not just tetrahedra."""
    output = tmp_path / "hybrid.su2"
    generate_mesh(capsule_request(capsule_step), output)

    element_types = set()
    with output.open() as handle:
        for line in handle:
            if line.startswith("NPOIN="):
                break
            parts = line.split()
            if len(parts) > 2 and parts[0].isdigit():
                element_types.add(int(parts[0]))

    assert 13 in element_types, "no prism (VTK type 13) cells in the mesh"
    assert 10 in element_types, "no tetrahedral (VTK type 10) cells in the mesh"


@pytest.mark.slow
@pytest.mark.parametrize("shape", [DomainShape.CYLINDER, DomainShape.BOX])
def test_both_domain_shapes_produce_a_farfield(capsule_step, tmp_path, shape):
    """Cylindrical and rectangular envelopes both mesh and tag correctly."""
    output = tmp_path / f"{shape.value}.su2"
    request = capsule_request(capsule_step, domain={"shape": shape})
    result = generate_mesh(request, output)
    assert result.boundary_markers[MARKER_FARFIELD] > 0
    assert result.cell_count > 0


@pytest.mark.slow
def test_nose_vector_orientation_is_applied(capsule_step, tmp_path):
    """Declaring the nose along -X still yields a +X-aligned mesh.

    The capsule is symmetric, so the recovered reference length is unchanged;
    what this checks is that the rotation path runs without destroying the
    geometry.
    """
    request = capsule_request(
        capsule_step, geometry={"nose_direction": AxisDirection.MINUS_X}
    )
    result = generate_mesh(request, tmp_path / "flipped.su2")
    assert result.reference_length_m == pytest.approx(0.58, rel=0.05)
    assert result.cell_count > 0


@pytest.mark.slow
def test_cell_count_targeting_reaches_the_requested_band(capsule_step, tmp_path):
    """The bisection is what makes the cell-count guarantee real.

    A deliberately narrow band around the achievable count is used so the test
    exercises the loop without needing a rocket-sized mesh.
    """
    request = capsule_request(capsule_step)
    baseline = generate_mesh(request, tmp_path / "baseline.su2")

    # Aim for roughly double the baseline and let the loop find the size.
    target = 2.0 * baseline.cell_count
    band = (int(0.6 * target), int(1.6 * target))

    import backend.mesh_pipeline as pipeline

    original = MeshParams.target_band
    MeshParams.target_band = lambda self: band  # type: ignore[assignment]
    try:
        tuned = generate_mesh(
            capsule_request(capsule_step, mesh={"max_targeting_iterations": 4}),
            tmp_path / "tuned.su2",
        )
    finally:
        MeshParams.target_band = original  # type: ignore[assignment]

    assert tuned.target_band == band
    assert tuned.within_target_band, (
        f"targeting ended at {tuned.cell_count} cells, outside {band} "
        f"after {tuned.targeting_iterations} iterations"
    )
    assert band[0] <= tuned.cell_count <= band[1]


@pytest.mark.slow
def test_a_millimetre_model_measures_the_same_as_its_metre_twin(tmp_path):
    """The same body drawn in millimetres yields the same metres out.

    Most CAD exports are in millimetres, and the geometry stage meshes in
    whatever unit the file uses -- converting the CAD instead fails on
    spline surfaces and, at metre scale, on Gmsh's periodic-surface mesher.
    The conversion happens on the finished node array, and this is what
    proves it lands in the same place.
    """
    path = tmp_path / "capsule_mm.step"
    with gmsh_session("capsule_mm"):
        occ = gmsh.model.occ
        barrel = occ.addCylinder(0.0, 0.0, 0.0, 500.0, 0.0, 0.0, 40.0)
        nose = occ.addSphere(0.0, 0.0, 0.0, 40.0)
        tail = occ.addSphere(500.0, 0.0, 0.0, 40.0)
        occ.fuse([(3, barrel)], [(3, nose), (3, tail)])
        occ.synchronize()
        gmsh.write(str(path))

    result = generate_mesh(
        capsule_request(path, geometry={"scale_to_meters": 0.001}),
        tmp_path / "capsule_mm.su2",
    )

    # 580 mm long, 80 mm across -- the same capsule the metre tests use.
    assert result.reference_length_m == pytest.approx(0.58, rel=0.05)
    assert result.reference_diameter_m == pytest.approx(0.08, rel=0.1)
    assert result.first_cell_height_m < 1.0e-3
    assert result.cell_count > 0


@pytest.mark.slow
def test_a_surface_only_body_that_closes_is_sewn_into_a_solid(tmp_path):
    """CAD exported as a skin is repaired rather than refused.

    A file with faces but no solid is a common export mistake. When those
    faces do enclose a volume, sewing and capping them costs nothing and
    saves a round trip through the operator's CAD package.
    """
    path = tmp_path / "shell_only.step"
    with gmsh_session("shell"):
        occ = gmsh.model.occ
        solid = occ.addCylinder(0.0, 0.0, 0.0, 0.5, 0.0, 0.0, 0.04)
        occ.synchronize()
        # Drop the volume, keep its faces: a skin, exactly as a surfacing
        # tool would write one.
        gmsh.model.occ.remove([(3, solid)], recursive=False)
        occ.synchronize()
        assert not gmsh.model.getEntities(3)
        gmsh.write(str(path))

    result = generate_mesh(capsule_request(path), tmp_path / "shell.su2")
    assert result.reference_length_m == pytest.approx(0.5, rel=0.05)
    assert result.cell_count > 0


@pytest.mark.parametrize(
    "message",
    [
        "Impossible to mesh periodic surface 8",
        "The 1D mesh seems not to be forming a closed loop",
    ],
)
def test_gmsh_closed_face_failures_are_recognised(message):
    """These two messages are the trigger for the split retry.

    They are matched on text because Gmsh raises the same exception type
    for every meshing failure, so a wording change here silently turns a
    recoverable geometry into a hard error.
    """
    assert _is_closed_face_failure(Exception(message))
    assert not _is_closed_face_failure(Exception("boundary layer inverted"))


def test_splitting_divides_a_body_without_changing_it():
    """The split is a repair; it must not cost any of the geometry."""
    report = HealingReport()
    with gmsh_session("split"):
        occ = gmsh.model.occ
        solid = occ.addCylinder(0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.05)
        occ.synchronize()
        before = occ.getMass(3, solid)

        assert _split_closed_faces([solid], report)

        pieces = [tag for _, tag in gmsh.model.getEntities(3)]
        assert len(pieces) > 1
        after = sum(occ.getMass(3, tag) for tag in pieces)
        assert after == pytest.approx(before, rel=1e-6)
    assert any("cut along two axial planes" in note for note in report.notes)


def test_a_split_that_loses_part_of_the_body_is_refused(monkeypatch):
    """A boolean that eats half the rocket must not reach the solver.

    This is the dangerous failure mode: the remains still mesh, and every
    force computed from them is quietly wrong.
    """
    report = HealingReport()
    with gmsh_session("split_loss"):
        occ = gmsh.model.occ
        keep = occ.addBox(0.0, -0.05, -0.05, 0.5, 0.1, 0.1)
        drop = occ.addBox(0.6, -0.05, -0.05, 0.5, 0.1, 0.1)
        occ.synchronize()

        def eat_half(objects, tools, **kwargs):
            """Stand in for a boolean that discards part of the body."""
            gmsh.model.occ.remove([(3, drop)], recursive=True)
            return [(3, keep)], []

        monkeypatch.setattr(gmsh.model.occ, "fragment", eat_half)
        with pytest.raises(MeshPipelineError, match="changed its extent"):
            _split_closed_faces([keep, drop], report)


def test_missing_step_file_is_reported_clearly(tmp_path):
    """A bad path fails with a useful message, not a Gmsh stack trace."""
    request = MeshRequest(
        geometry=GeometryParams(step_file_path=str(tmp_path / "absent.step"))
    )
    with pytest.raises(MeshPipelineError, match="STEP file not found"):
        generate_mesh(request, tmp_path / "out.su2")


def test_step_file_without_solids_is_reported_clearly(tmp_path):
    """Surface-only CAD cannot be meshed and must say so.

    OpenCASCADE healing can also delete a solid outright; the pipeline rolls
    that back and only then reports the failure.
    """
    path = tmp_path / "surface_only.step"
    with gmsh_session("surface"):
        occ = gmsh.model.occ
        occ.addRectangle(0.0, 0.0, 0.0, 1.0, 1.0)
        occ.synchronize()
        gmsh.write(str(path))

    request = MeshRequest(geometry=GeometryParams(step_file_path=str(path)))
    with pytest.raises(MeshPipelineError, match="no solid"):
        generate_mesh(request, tmp_path / "out.su2")


# ---------------------------------------------------------------------------
# Not repeating work we already know will fail
# ---------------------------------------------------------------------------


def test_the_split_is_discovered_once_not_once_per_attempt(monkeypatch, tmp_path):
    """Learning that a body needs splitting should be paid for once.

    The cell-count loop runs up to five times. The operator's rocket has a
    face that closes on itself, so every attempt used to re-import the CAD,
    re-classify its faces and re-run the surface mesh that was always going
    to fail, before splitting and starting again.
    """
    import backend.mesh_pipeline as pipeline

    calls: list[bool] = []
    real = pipeline._build_surface_mesh

    def counted(request, size_scale, notify, split):
        calls.append(split)
        if not split:
            raise RuntimeError("Impossible to mesh periodic surface 8")
        return real(request, size_scale, notify, split=False)

    monkeypatch.setattr(pipeline, "_build_surface_mesh", counted)

    request = capsule_request(
        capsule_step_for(tmp_path), mesh={"max_targeting_iterations": 2}
    )
    pipeline.generate_mesh(request, tmp_path / "sticky.su2")

    # One doomed attempt at the start, and never again.
    assert calls.count(False) == 1
    assert len(calls) > 2, "the targeting loop did not run more than once"


def capsule_step_for(tmp_path):
    """A capsule written fresh, for tests that cannot share the module fixture."""
    path = tmp_path / "capsule_local.step"
    with gmsh_session("capsule_local"):
        occ = gmsh.model.occ
        barrel = occ.addCylinder(0.0, 0.0, 0.0, 0.5, 0.0, 0.0, 0.04)
        nose = occ.addSphere(0.0, 0.0, 0.0, 0.04)
        occ.fuse([(3, barrel)], [(3, nose)])
        occ.synchronize()
        gmsh.write(str(path))
    return path


def test_a_body_that_fails_even_split_is_not_retried_forever(monkeypatch, tmp_path):
    """Splitting is one repair, not a loop to spin in."""
    import backend.mesh_pipeline as pipeline

    attempts = {"n": 0}

    def always_fails(request, size_scale, notify, split):
        attempts["n"] += 1
        raise RuntimeError("Impossible to mesh periodic surface 8")

    monkeypatch.setattr(pipeline, "_build_surface_mesh", always_fails)
    with pytest.raises(RuntimeError):
        pipeline.generate_mesh(
            capsule_request(capsule_step_for(tmp_path)), tmp_path / "x.su2"
        )
    assert attempts["n"] == 2


def test_the_recovery_is_not_reported_as_a_failure(monkeypatch, tmp_path):
    """It always recovers, and the word "failed" made operators think otherwise."""
    import backend.mesh_pipeline as pipeline

    real = pipeline._build_surface_mesh
    state = {"first": True}

    def once(request, size_scale, notify, split):
        if state["first"] and not split:
            state["first"] = False
            raise RuntimeError("Impossible to mesh periodic surface 8")
        return real(request, size_scale, notify, split=False)

    monkeypatch.setattr(pipeline, "_build_surface_mesh", once)
    lines: list[str] = []
    pipeline.generate_mesh(
        capsule_request(
            capsule_step_for(tmp_path), mesh={"max_targeting_iterations": 0}
        ),
        tmp_path / "worded.su2",
        progress=lines.append,
    )

    recovery = [line for line in lines if "closes on itself" in line]
    assert recovery, "the recovery was not explained"
    assert not any("failed" in line for line in recovery)


@pytest.mark.slow
def test_each_step_reports_how_long_it_took(capsule_step, tmp_path):
    """A four-minute step that says nothing is indistinguishable from a hang."""
    lines: list[str] = []
    generate_mesh(
        capsule_request(capsule_step), tmp_path / "timed.su2", progress=lines.append
    )

    for step in (
        "generating surface mesh",
        "generating tetrahedral farfield mesh",
        "writing SU2 mesh",
    ):
        assert any(line.startswith(f"{step} …") for line in lines), f"no start: {step}"
        assert any(
            line.startswith(f"{step} —") and line.endswith(" s") for line in lines
        ), f"no duration: {step}"


@pytest.mark.slow
def test_the_prism_march_reports_each_layer(capsule_step, tmp_path):
    """The stack takes minutes on a real body; per-layer is the honest unit."""
    lines: list[str] = []
    request = capsule_request(capsule_step, mesh={"boundary_layers": 5})
    generate_mesh(request, tmp_path / "layers.su2", progress=lines.append)

    layer_lines = [line for line in lines if line.strip().startswith("prism layer")]
    assert len(layer_lines) == 5
    assert "1 of 5" in layer_lines[0]
    assert "5 of 5" in layer_lines[-1]


# ---------------------------------------------------------------------------
# A quick look at the geometry
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_a_preview_comes_back_aligned_nose_first(tmp_path):
    """The preview is a check on the nose direction, not decoration.

    Showing the model in the frame the solver will use means a rocket that
    would fly tail-first is visible before anyone pays for a mesh.
    """
    from backend.mesh_pipeline import tessellate_geometry
    from backend.sample_geometry import create_reference_rocket_step

    path = create_reference_rocket_step(tmp_path / "rocket.step")
    preview = tessellate_geometry(
        GeometryParams(step_file_path=str(path), nose_direction=AxisDirection.PLUS_X)
    )

    assert len(preview.triangles) > 500
    assert preview.points.shape[1] == 3
    # The tapering end must sit at minimum X, where the flow arrives.
    low, high = preview.points[:, 0].min(), preview.points[:, 0].max()
    margin = 0.1 * (high - low)
    nose_radius = np.linalg.norm(
        preview.points[preview.points[:, 0] <= low + margin][:, 1:], axis=1
    ).mean()
    tail_radius = np.linalg.norm(
        preview.points[preview.points[:, 0] >= high - margin][:, 1:], axis=1
    ).mean()
    assert nose_radius < tail_radius


@pytest.mark.slow
def test_a_preview_is_measured_in_metres(tmp_path):
    """Millimetre CAD must come back the size it really is."""
    from backend.mesh_pipeline import tessellate_geometry

    path = tmp_path / "capsule_mm.step"
    with gmsh_session("preview_mm"):
        occ = gmsh.model.occ
        occ.addCylinder(0.0, 0.0, 0.0, 500.0, 0.0, 0.0, 40.0)
        occ.synchronize()
        gmsh.write(str(path))

    preview = tessellate_geometry(
        GeometryParams(step_file_path=str(path), scale_to_meters=0.001)
    )
    span = preview.points.max(axis=0) - preview.points.min(axis=0)
    assert span[0] == pytest.approx(0.5, rel=0.05)


@pytest.mark.slow
def test_a_preview_handles_a_face_that_closes_on_itself(tmp_path):
    """The preview meets the same geometry the mesher does, so it splits too."""
    from backend.mesh_pipeline import tessellate_geometry

    path = tmp_path / "closed.step"
    with gmsh_session("preview_closed"):
        occ = gmsh.model.occ
        occ.addCylinder(0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.05)
        occ.synchronize()
        gmsh.write(str(path))

    preview = tessellate_geometry(GeometryParams(step_file_path=str(path)))
    assert len(preview.triangles) > 100


def test_a_file_with_no_geometry_is_reported_not_crashed_on(tmp_path):
    """OpenCASCADE aborts the whole process on a re-import of an empty file.

    That used to take the application down with it, and the preview made it
    happen on import rather than on demand. A sentence is the right answer.
    """
    from backend.mesh_pipeline import tessellate_geometry
    from tests.test_step_inspect import MILLIMETRES, write_step

    path = write_step(tmp_path / "empty.step", MILLIMETRES, [(0, 0, 0), (10, 1, 1)])
    with pytest.raises(MeshPipelineError, match="no geometry"):
        tessellate_geometry(GeometryParams(step_file_path=path))


def _marker_centroids(path, marker):
    """Centroids of one marker's triangles in a written .su2 file."""
    lines = Path(path).read_text().split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("NPOIN"))
    count = int(lines[start].split("=")[1].split()[0])
    points = np.array(
        [list(map(float, line.split()[:3])) for line in lines[start + 1 : start + 1 + count]]
    )
    index = next(i for i, line in enumerate(lines) if line.strip() == f"MARKER_TAG= {marker}")
    elements = int(lines[index + 1].split("=")[1])
    triangles = np.array(
        [list(map(int, line.split()[1:4])) for line in lines[index + 2 : index + 2 + elements]]
    )
    return points[triangles].mean(axis=1)


@pytest.mark.slow
def test_fin_and_body_triangles_land_in_their_own_markers(tmp_path):
    """The markers are cut from the wall triangles by count, so order matters.

    Concatenating in CAD face order interleaved body and fin faces, and a
    real rocket came out with thousands of nose and body triangles in
    WALL_FINS -- which is what a fin hinge torque restricted to the fins
    would have integrated.
    """
    from backend.mesh_pipeline import MARKER_WALL_FINS
    from backend.sample_geometry import create_reference_rocket_step

    step = create_reference_rocket_step(tmp_path / "rocket.step")
    output = tmp_path / "rocket.su2"
    generate_mesh(
        MeshRequest(
            geometry=GeometryParams(step_file_path=str(step)),
            domain=DomainParams(),
            mesh=MeshParams(boundary_layers=5, max_targeting_iterations=0),
            sizing_flow=FlowParams(velocity_value=1.3),
        ),
        output,
    )
    body = _marker_centroids(output, MARKER_WALL_ROCKET)
    fins = _marker_centroids(output, MARKER_WALL_FINS)
    body_radius = 0.04  # the sample's 80 mm airframe
    # No fin surface in the body marker, no nose or body tube in the fins.
    assert np.hypot(body[:, 1], body[:, 2]).max() < 1.1 * body_radius
    assert fins[:, 0].min() > 0.6  # fins sit on the aft 40% of the 1 m body


def test_a_self_crossing_prism_shell_is_retried_with_fewer_layers(monkeypatch, tmp_path):
    """A shell the tet mesher rejects costs a layer, not the whole mesh.

    The operator's rocket at Mach 0.3 with seven layers failed outright with
    "PLC Error: A segment and a facet intersect", and no domain shape or size
    the assistant tried could get past it.
    """
    import backend.mesh_pipeline as pipeline

    real = pipeline._mesh_farfield
    layers_seen: list[int] = []

    def rejects_the_first_shell(request, metrics, prisms, notify, size_scale=1.0):
        layers_seen.append(prisms.layers_requested)
        if len(layers_seen) == 1:
            raise MeshPipelineError(
                "farfield tetrahedralisation failed: PLC Error:  A segment "
                "and a facet intersect at point"
            )
        return real(request, metrics, prisms, notify, size_scale)

    monkeypatch.setattr(pipeline, "_mesh_farfield", rejects_the_first_shell)
    request = capsule_request(
        capsule_step_for(tmp_path),
        mesh={"max_targeting_iterations": 0, "boundary_layers": 6},
    )
    result = pipeline.generate_mesh(request, tmp_path / "fewer.su2")
    assert layers_seen == [6, 5]
    assert result.cell_count > 0


def test_other_farfield_failures_are_not_retried(monkeypatch, tmp_path):
    """Only a crossing shell is helped by a thinner stack."""
    import backend.mesh_pipeline as pipeline

    calls = {"n": 0}

    def broken(request, metrics, prisms, notify, size_scale=1.0):
        calls["n"] += 1
        raise MeshPipelineError("farfield tetrahedralisation failed: out of memory")

    monkeypatch.setattr(pipeline, "_mesh_farfield", broken)
    request = capsule_request(
        capsule_step_for(tmp_path), mesh={"max_targeting_iterations": 0}
    )
    with pytest.raises(MeshPipelineError, match="out of memory"):
        pipeline.generate_mesh(request, tmp_path / "broken.su2")
    assert calls["n"] == 1


def test_prisms_that_lost_a_column_are_written_as_pyramids_and_tets():
    """A prism naming the same node twice is what SU2 calls distorted."""
    from backend.mesh_pipeline import _tet_volumes, split_collapsed_wedges

    points = np.array(
        [
            [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0],   # base
            [0.0, 0.0, 1.0], [1.0, 0.0, 1.0], [0.0, 1.0, 1.0],   # full top
        ]
    )
    # Base wound towards the top, as the extruder builds it.
    intact = [0, 1, 2, 3, 4, 5]
    one_down = [0, 1, 2, 0, 4, 5]
    two_down = [0, 1, 2, 0, 1, 5]
    wedges, pyramids, tets = split_collapsed_wedges(
        points, np.array([intact, one_down, two_down])
    )
    assert wedges.tolist() == [intact]
    assert len(pyramids) == 1 and len(tets) == 1
    assert sorted(pyramids[0].tolist()) == [0, 1, 2, 4, 5]
    assert pyramids[0][4] == 0, "the collapsed column is the apex"
    assert sorted(tets[0].tolist()) == [0, 1, 2, 5]

    pyramid_volume = _tet_volumes(points, pyramids[:, [0, 1, 2, 4]]) + _tet_volumes(
        points, pyramids[:, [0, 2, 3, 4]]
    )
    assert pyramid_volume[0] > 0.0
    assert _tet_volumes(points, tets)[0] > 0.0


def test_the_nose_tip_is_put_at_the_origin(tmp_path):
    """Distances are read from the nose, wherever the CAD origin was.

    The Sapphire's CAD origin sat at its tail, so a centre of pressure 0.83 m
    behind the nose came back as 0.47 m ahead of the origin.
    """
    from backend.mesh_pipeline import tessellate_geometry

    step = capsule_step_for(tmp_path)  # hemisphere nose reaching x = -0.04
    preview = tessellate_geometry(
        GeometryParams(step_file_path=str(step), nose_direction="+X"), 2000
    )
    low, high = preview.points.min(axis=0), preview.points.max(axis=0)
    # Within a millimetre: a coarse triangulation cuts the curved tip short.
    assert abs(low[0]) < 1e-3
    assert abs(high[0] - 0.54) < 1e-3
    assert abs(low[1] + high[1]) < 1e-3 and abs(low[2] + high[2]) < 1e-3
