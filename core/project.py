"""Project files: one document holding a complete simulation setup.

A project bundles every parameter of both tracks -- geometry, farfield domain,
meshing, flight condition, solver numerics, fin hinges, sensor scenario and
sweep definition -- into a single ``.atsproj`` file. Opening one restores the
whole application state, which is what lets a study be put down and picked up
weeks later, handed to a colleague, or committed alongside the CAD it refers
to.

The file is plain JSON built from the same models the GUI and the MCP server
use, so it is readable, diffable and forward-compatible: a format version is
recorded, unknown-but-newer files are refused rather than silently misread,
and a field added to a model appears in saved projects automatically.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator

from core import PROJECT_FORMAT_VERSION, __version__
from core.models import (
    AeroRunRequest,
    DomainParams,
    FlowParams,
    GeometryParams,
    HingeAxis,
    MeshParams,
    MeshRequest,
    ReferenceValues,
    SolverParams,
    StrictModel,
    ThermalParams,
)

PROJECT_EXTENSION = ".atsproj"
PROJECT_FILTER = "AeroThermalStudio project (*.atsproj)"


class ProjectError(RuntimeError):
    """Raised when a project file cannot be read, written or understood."""


def _utc_now() -> str:
    """Current UTC timestamp in ISO-8601 form."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProjectMetadata(StrictModel):
    """Provenance recorded with every project."""

    name: str = Field(
        default="Untitled project",
        min_length=1,
        description="Human-readable project name shown in the title bar.",
    )
    description: str = Field(
        default="",
        description="Free-text description of what this study investigates.",
    )
    author: str = Field(default="", description="Who created the project.")
    created_at: str = Field(
        default_factory=_utc_now, description="ISO-8601 creation timestamp."
    )
    modified_at: str = Field(
        default_factory=_utc_now, description="ISO-8601 last-saved timestamp."
    )
    format_version: int = Field(
        default=PROJECT_FORMAT_VERSION,
        ge=1,
        description=(
            "On-disk format version. A file newer than the application is "
            "refused rather than partially read."
        ),
    )
    app_version: str = Field(
        default=__version__,
        description="Version of AeroThermalStudio that wrote the file.",
    )


class SweepSettings(StrictModel):
    """Definition of a parametric sweep, stored with the project."""

    parameter: str = Field(
        default="mach",
        description="Parameter to vary: 'mach', 'aoa', 'sideslip' or 'altitude'.",
    )
    values: list[float] = Field(
        default_factory=lambda: [0.5, 1.0, 1.5, 2.0, 2.5],
        description="Values to run, one simulation per entry.",
    )
    stop_on_error: bool = Field(
        default=False,
        description="Abort the batch at the first failure instead of continuing.",
    )

    def values_text(self) -> str:
        """The value list as the comma-separated text the GUI edits."""
        return ", ".join(f"{value:g}" for value in self.values)

    @classmethod
    def from_text(cls, parameter: str, text: str, **kwargs: Any) -> "SweepSettings":
        """Parse a comma-separated value list from the GUI.

        Raises
        ------
        ProjectError
            If any entry is not a number, naming the offending token.
        """
        values: list[float] = []
        for token in text.replace(";", ",").split(","):
            token = token.strip()
            if not token:
                continue
            try:
                values.append(float(token))
            except ValueError as error:
                raise ProjectError(
                    f"'{token}' is not a number; sweep values must be a "
                    "comma-separated list such as '0.5, 1.0, 1.5'"
                ) from error
        return cls(parameter=parameter, values=values, **kwargs)


class Project(StrictModel):
    """A complete, saveable simulation setup.

    Every section carries a default, so a new project is immediately valid and
    the operator only changes what matters to them. ``geometry`` and
    ``thermal`` are optional because a project may concern only one of the two
    tracks.
    """

    metadata: ProjectMetadata = Field(default_factory=ProjectMetadata)
    geometry: GeometryParams | None = Field(
        default=None,
        description="CAD file and orientation. Null until a STEP file is chosen.",
    )
    domain: DomainParams = Field(default_factory=DomainParams)
    mesh: MeshParams = Field(default_factory=MeshParams)
    flow: FlowParams = Field(
        default_factory=lambda: FlowParams(velocity_value=2.0),
        description="Flight condition for the aerodynamic track.",
    )
    solver: SolverParams = Field(default_factory=SolverParams)
    reference: ReferenceValues = Field(default_factory=ReferenceValues)
    hinge_axes: list[HingeAxis] = Field(
        default_factory=list, description="Fin hinge axes to evaluate torque about."
    )
    thermal: ThermalParams | None = Field(
        default=None,
        description="Sensor microclimate scenario. Null for aerodynamic-only projects.",
    )
    sweep: SweepSettings = Field(default_factory=SweepSettings)
    notes: str = Field(
        default="",
        description="Operator's working notes, carried with the project.",
    )
    last_mesh_id: str | None = Field(
        default=None,
        description=(
            "Identifier of the mesh most recently generated for this project, "
            "so reopening it can offer to reuse that mesh."
        ),
    )

    # -- persistence -------------------------------------------------------

    def save(self, path: Path | str) -> Path:
        """Write the project to disk as JSON.

        The modification timestamp is refreshed and the extension added if the
        caller omitted it. The write goes to a temporary file first and is
        then moved into place, so an interrupted save cannot leave a
        half-written project behind.

        Parameters
        ----------
        path:
            Destination file. ``.atsproj`` is appended when missing.

        Returns
        -------
        Path
            The path actually written.
        """
        path = Path(path)
        if path.suffix.lower() != PROJECT_EXTENSION:
            path = path.with_suffix(PROJECT_EXTENSION)

        self.metadata.modified_at = _utc_now()
        self.metadata.app_version = __version__
        self.metadata.format_version = PROJECT_FORMAT_VERSION

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                self.model_dump_json(indent=2), encoding="utf-8"
            )
            temporary.replace(path)
        except OSError as error:
            raise ProjectError(f"could not save '{path.name}': {error}") from error
        return path

    @classmethod
    def load(cls, path: Path | str) -> "Project":
        """Read a project from disk.

        Raises
        ------
        ProjectError
            If the file is missing, is not valid JSON, was written by a newer
            version of the format, or does not match the current models.
        """
        path = Path(path)
        if not path.is_file():
            raise ProjectError(f"project file not found: {path}")

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ProjectError(
                f"'{path.name}' is not a valid project file: {error}"
            ) from error
        except OSError as error:
            raise ProjectError(f"could not read '{path.name}': {error}") from error

        if not isinstance(payload, dict):
            raise ProjectError(f"'{path.name}' does not contain a project")

        version = int(
            (payload.get("metadata") or {}).get(
                "format_version", PROJECT_FORMAT_VERSION
            )
        )
        if version > PROJECT_FORMAT_VERSION:
            raise ProjectError(
                f"'{path.name}' was written in project format v{version}, but "
                f"this version of AeroThermalStudio understands up to "
                f"v{PROJECT_FORMAT_VERSION}. Update the application to open it."
            )

        try:
            return cls(**payload)
        except ValidationError as error:
            raise ProjectError(
                f"'{path.name}' could not be loaded: {_first_validation_message(error)}"
            ) from error

    # -- derived requests --------------------------------------------------

    def to_mesh_request(self) -> MeshRequest:
        """Build the meshing request this project describes.

        Raises
        ------
        ProjectError
            If no CAD file has been selected yet.
        """
        if self.geometry is None:
            raise ProjectError(
                "this project has no CAD file selected; choose a STEP file "
                "before meshing"
            )
        return MeshRequest(
            geometry=self.geometry,
            domain=self.domain,
            mesh=self.mesh,
            sizing_flow=self.flow,
        )

    def to_aero_request(self, mesh_id: str) -> AeroRunRequest:
        """Build the aerodynamic run request for a given mesh."""
        return AeroRunRequest(
            mesh_id=mesh_id,
            flow=self.flow,
            solver=self.solver,
            reference=self.reference,
            hinge_axes=list(self.hinge_axes),
        )

    def summary(self) -> dict[str, Any]:
        """A short description for listings and the recent-projects menu."""
        return {
            "name": self.metadata.name,
            "description": self.metadata.description,
            "modified_at": self.metadata.modified_at,
            "has_geometry": self.geometry is not None,
            "has_thermal": self.thermal is not None,
            "step_file": self.geometry.step_file_path if self.geometry else None,
            "mach": self.flow.mach(),
            "aoa_deg": self.flow.aoa_deg,
            "hinge_count": len(self.hinge_axes),
            "mesh_resolution": self.mesh.resolution.value,
            "sweep_parameter": self.sweep.parameter,
            "sweep_points": len(self.sweep.values),
        }

    def touch(self) -> None:
        """Mark the project as modified now."""
        self.metadata.modified_at = _utc_now()


def _first_validation_message(error: ValidationError) -> str:
    """Turn a pydantic error into one readable sentence."""
    issues = error.errors()
    if not issues:  # pragma: no cover - pydantic always reports at least one
        return str(error)
    first = issues[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "(root)"
    return f"{location}: {first.get('msg', 'invalid value')}"


def default_project(name: str = "Untitled project") -> Project:
    """A fresh project preloaded with the reference scenarios.

    The thermal section is populated with the tram-roof case so the sensor tab
    has something meaningful to show the moment the application opens.
    """
    return Project(
        metadata=ProjectMetadata(name=name),
        thermal=ThermalParams(
            enclosure_step_path="",
            sensor_xyz=[0.030, 0.020, 0.015],
        ),
    )


def projects_directory(root: Path | str | None = None) -> Path:
    """Directory projects are stored in by default.

    Sits beside the run registry under the application data root, so meshes,
    results and the projects that produced them stay together.
    """
    from core.platform_env import data_root

    base = Path(root) if root is not None else data_root()
    directory = base / "projects"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def list_projects(directory: Path | str | None = None) -> list[dict[str, Any]]:
    """Summarise every project in a directory, newest first.

    Unreadable files are reported with their error rather than omitted, so a
    corrupted project is visible instead of silently missing.
    """
    directory = Path(directory) if directory is not None else projects_directory()
    entries: list[dict[str, Any]] = []

    for path in sorted(directory.glob(f"*{PROJECT_EXTENSION}")):
        entry: dict[str, Any] = {"path": str(path), "filename": path.name}
        try:
            entry.update(Project.load(path).summary())
            entry["readable"] = True
        except ProjectError as error:
            entry["readable"] = False
            entry["error"] = str(error)
            entry["name"] = path.stem
            entry["modified_at"] = ""
        entries.append(entry)

    entries.sort(key=lambda item: item.get("modified_at", ""), reverse=True)
    return entries
