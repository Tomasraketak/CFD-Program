"""Assembly and serialisation of SU2 native (``.su2``) meshes.

The mesh this platform produces comes from two sources: prism layers built by
:mod:`backend.prism_layers` and tetrahedra built by Gmsh against the prism
outer shell. Neither Gmsh's exporter nor a single model can express that
combination, so the two are stitched together here and written directly.

The ``.su2`` format is plain ASCII and element types use VTK numbering, which
makes a hand-written writer both simple and far easier to control than
coercing an exporter into emitting the marker tags SU2 needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np

# VTK element type identifiers, which SU2 reuses verbatim.
VTK_LINE = 3
VTK_TRIANGLE = 5
VTK_QUAD = 9
VTK_TETRA = 10
VTK_HEXA = 12
VTK_WEDGE = 13
VTK_PYRAMID = 14

# Nodes per element for each supported type.
NODES_PER_ELEMENT: dict[int, int] = {
    VTK_LINE: 2,
    VTK_TRIANGLE: 3,
    VTK_QUAD: 4,
    VTK_TETRA: 4,
    VTK_HEXA: 8,
    VTK_WEDGE: 6,
    VTK_PYRAMID: 5,
}

# Element types that occupy volume, used for the cell-count report.
VOLUME_ELEMENT_TYPES = frozenset({VTK_TETRA, VTK_HEXA, VTK_WEDGE, VTK_PYRAMID})


class MeshAssemblyError(RuntimeError):
    """Raised when an assembled mesh is inconsistent or unwritable."""


@dataclass
class ElementBlock:
    """A homogeneous block of elements of one VTK type.

    Attributes
    ----------
    element_type:
        VTK type identifier.
    connectivity:
        ``(E, nodes_per_element)`` zero-based node indices.
    """

    element_type: int
    connectivity: np.ndarray

    def __post_init__(self) -> None:
        self.connectivity = np.ascontiguousarray(self.connectivity, dtype=np.int64)
        expected = NODES_PER_ELEMENT.get(self.element_type)
        if expected is None:
            raise MeshAssemblyError(
                f"unsupported element type {self.element_type}"
            )
        if self.connectivity.ndim != 2 or self.connectivity.shape[1] != expected:
            raise MeshAssemblyError(
                f"element type {self.element_type} needs {expected} nodes per "
                f"element, got array of shape {self.connectivity.shape}"
            )

    @property
    def count(self) -> int:
        """Number of elements in this block."""
        return int(self.connectivity.shape[0])

    @property
    def is_volume(self) -> bool:
        """True when this block contributes cells rather than faces."""
        return self.element_type in VOLUME_ELEMENT_TYPES


@dataclass
class SU2Mesh:
    """A complete SU2 mesh: nodes, volume elements and tagged boundaries.

    Attributes
    ----------
    points:
        ``(N, 3)`` node coordinates.
    volume_blocks:
        Volume element blocks (tetrahedra, prisms, pyramids).
    markers:
        Mapping of marker name to the surface element blocks forming it.
        Marker names are what the solver configuration references, e.g.
        ``WALL_ROCKET`` or ``FARFIELD``.
    """

    points: np.ndarray
    volume_blocks: list[ElementBlock] = field(default_factory=list)
    markers: dict[str, list[ElementBlock]] = field(default_factory=dict)
    dimension: int = 3

    def __post_init__(self) -> None:
        self.points = np.ascontiguousarray(self.points, dtype=float)
        if self.points.ndim != 2 or self.points.shape[1] != 3:
            raise MeshAssemblyError(
                f"points must be an (N, 3) array, got {self.points.shape}"
            )

    @property
    def node_count(self) -> int:
        """Number of nodes."""
        return int(self.points.shape[0])

    @property
    def cell_count(self) -> int:
        """Total number of volume cells."""
        return sum(block.count for block in self.volume_blocks)

    def cell_counts_by_type(self) -> dict[str, int]:
        """Cell counts keyed by readable element-type name."""
        names = {
            VTK_TETRA: "tetrahedra",
            VTK_WEDGE: "prisms",
            VTK_PYRAMID: "pyramids",
            VTK_HEXA: "hexahedra",
        }
        counts: dict[str, int] = {}
        for block in self.volume_blocks:
            key = names.get(block.element_type, str(block.element_type))
            counts[key] = counts.get(key, 0) + block.count
        return counts

    def marker_counts(self) -> dict[str, int]:
        """Surface element count per marker, for the boundary report."""
        return {
            name: sum(block.count for block in blocks)
            for name, blocks in self.markers.items()
        }

    def add_volume(self, element_type: int, connectivity: np.ndarray) -> None:
        """Append a block of volume elements."""
        block = ElementBlock(element_type, connectivity)
        if not block.is_volume:
            raise MeshAssemblyError(
                f"element type {element_type} is not a volume element"
            )
        if block.count:
            self.volume_blocks.append(block)

    def add_marker(
        self, name: str, element_type: int, connectivity: np.ndarray
    ) -> None:
        """Append surface elements to a named boundary marker."""
        block = ElementBlock(element_type, connectivity)
        if block.is_volume:
            raise MeshAssemblyError(
                f"element type {element_type} is a volume element and cannot "
                f"form boundary marker '{name}'"
            )
        if block.count:
            self.markers.setdefault(name, []).append(block)

    def validate(self) -> None:
        """Check internal consistency before writing.

        Raises
        ------
        MeshAssemblyError
            If any element references a node outside the node array, if there
            are no volume cells, or if a marker is empty.
        """
        if self.node_count == 0:
            raise MeshAssemblyError("mesh has no nodes")
        if self.cell_count == 0:
            raise MeshAssemblyError("mesh has no volume cells")
        if not np.all(np.isfinite(self.points)):
            raise MeshAssemblyError("mesh contains non-finite node coordinates")

        limit = self.node_count
        for block in self.volume_blocks:
            _check_indices(block, limit, "volume")
        for name, blocks in self.markers.items():
            if not blocks or all(block.count == 0 for block in blocks):
                raise MeshAssemblyError(f"marker '{name}' contains no elements")
            for block in blocks:
                _check_indices(block, limit, f"marker '{name}'")

    def write(self, path: Path | str) -> Path:
        """Serialise to a ``.su2`` file.

        Parameters
        ----------
        path:
            Destination file path. Parent directories are created.

        Returns
        -------
        Path
            The written path.
        """
        self.validate()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("w", encoding="ascii", newline="\n") as handle:
            handle.write(f"NDIME= {self.dimension}\n")

            handle.write(f"NELEM= {self.cell_count}\n")
            index = 0
            for block in self.volume_blocks:
                index = _write_elements(handle, block, index, with_index=True)

            handle.write(f"NPOIN= {self.node_count}\n")
            # 17 significant digits round-trips an IEEE double exactly. The
            # explicit float() matters: repr of a NumPy scalar would emit
            # "np.float64(0.0)" and produce a file SU2 cannot parse.
            for node_index, (x, y, z) in enumerate(self.points):
                handle.write(
                    f"{float(x):.17g} {float(y):.17g} {float(z):.17g} "
                    f"{node_index}\n"
                )

            handle.write(f"NMARK= {len(self.markers)}\n")
            for name, blocks in self.markers.items():
                total = sum(block.count for block in blocks)
                handle.write(f"MARKER_TAG= {name}\n")
                handle.write(f"MARKER_ELEMS= {total}\n")
                for block in blocks:
                    _write_elements(handle, block, 0, with_index=False)

        return path


def _check_indices(block: ElementBlock, limit: int, where: str) -> None:
    """Raise when a block references nodes outside the node array."""
    if block.count == 0:
        return
    smallest = int(block.connectivity.min())
    largest = int(block.connectivity.max())
    if smallest < 0 or largest >= limit:
        raise MeshAssemblyError(
            f"{where} references node index range [{smallest}, {largest}] "
            f"outside the {limit} available nodes"
        )


def _write_elements(handle, block: ElementBlock, index: int, with_index: bool) -> int:
    """Write one element block, returning the next running index."""
    element_type = block.element_type
    for nodes in block.connectivity:
        row = " ".join(str(int(n)) for n in nodes)
        if with_index:
            handle.write(f"{element_type} {row} {index}\n")
            index += 1
        else:
            handle.write(f"{element_type} {row}\n")
    return index


def merge_nodes(
    blocks: Iterable[np.ndarray], tolerance: float = 1.0e-12
) -> tuple[np.ndarray, list[np.ndarray]]:
    """Concatenate point blocks, fusing coincident nodes.

    Used to stitch the prism stack onto the tetrahedral region, where the
    shell nodes appear in both. Exact duplicates are expected because the tet
    mesher is fed the prism shell verbatim, but a tolerance is accepted for
    robustness.

    Parameters
    ----------
    blocks:
        Point arrays to merge, in order.
    tolerance:
        Distance below which two nodes are considered the same.

    Returns
    -------
    tuple
        The merged ``(N, 3)`` points and, for each input block, an index array
        mapping its original rows onto rows of the merged array.
    """
    blocks = [np.ascontiguousarray(b, dtype=float) for b in blocks]
    if not blocks:
        return np.empty((0, 3)), []

    stacked = np.concatenate(blocks, axis=0)
    if tolerance <= 0.0:
        unique, inverse = np.unique(stacked, axis=0, return_inverse=True)
    else:
        # Quantise onto a grid so coincident-within-tolerance nodes collapse.
        quantised = np.round(stacked / tolerance).astype(np.int64)
        _, first_occurrence, inverse = np.unique(
            quantised, axis=0, return_index=True, return_inverse=True
        )
        unique = stacked[first_occurrence]

    inverse = np.asarray(inverse).ravel()
    mappings: list[np.ndarray] = []
    offset = 0
    for block in blocks:
        mappings.append(inverse[offset : offset + len(block)])
        offset += len(block)
    return unique, mappings


def triangles_to_marker(
    triangles: np.ndarray, mapping: np.ndarray | None = None
) -> np.ndarray:
    """Remap a triangulation onto merged node indices."""
    triangles = np.asarray(triangles, dtype=np.int64)
    if mapping is None:
        return triangles
    return np.asarray(mapping, dtype=np.int64)[triangles]


def read_su2_header(path: Path | str) -> Mapping[str, object]:
    """Read counts and marker names from a ``.su2`` file without loading it.

    Cheap enough to call on an existing mesh to populate a report, and used by
    the tests to verify what was written.
    """
    path = Path(path)
    dimension = 0
    cell_count = 0
    node_count = 0
    markers: dict[str, int] = {}

    current_marker: str | None = None
    with path.open("r", encoding="ascii") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith("NDIME="):
                dimension = int(stripped.split("=")[1])
            elif stripped.startswith("NELEM="):
                cell_count = int(stripped.split("=")[1])
            elif stripped.startswith("NPOIN="):
                node_count = int(stripped.split("=")[1])
            elif stripped.startswith("MARKER_TAG="):
                current_marker = stripped.split("=")[1].strip()
            elif stripped.startswith("MARKER_ELEMS=") and current_marker:
                markers[current_marker] = int(stripped.split("=")[1])

    return {
        "dimension": dimension,
        "cell_count": cell_count,
        "node_count": node_count,
        "markers": markers,
    }
