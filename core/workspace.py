"""The CAD file the operator currently has open.

The desktop application and the AI assistant are two ways of driving one
workstation, not two programs that happen to share a disk. When someone
picks a STEP file in the interface, "mesh what I just imported" has to mean
something to the assistant as well — otherwise the operator ends up reading
a path off the title bar and typing it back in, which is what this module
exists to stop.

So the choice is recorded here, in one small file beside the run registry,
and the tools read it when no path is given. It is a pointer, not a
project: a project holds a whole study and is saved deliberately, while
this is just "the file on the bench right now" and is overwritten the
moment another is chosen.

The file is written by whichever surface made the choice, and read by both.
A missing or damaged one means nothing is loaded, never an error, because
the only cost of losing it is having to name the file again.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError

from core.models import StrictModel
from core.platform_env import data_root
from core.step_inspect import (
    StepInspectionError,
    describe_length_unit,
    suggest_scale_to_meters,
)

WORKSPACE_FILENAME = "active_geometry.json"


class ActiveGeometry(StrictModel):
    """The CAD file most recently loaded, and what is known about it."""

    step_file_path: str = Field(
        description="Absolute path to the .step/.stp file on this machine."
    )
    scale_to_meters: float = Field(
        default=1.0,
        gt=0.0,
        description=(
            "Multiplier converting the file's coordinates to metres, read "
            "from the unit the file declares and checked against its size."
        ),
    )
    scale_is_confident: bool = Field(
        default=False,
        description=(
            "Whether the unit was established rather than guessed. False "
            "means the operator should confirm it before a solve is paid for."
        ),
    )
    scale_reason: str = Field(
        default="",
        description="How the scale was arrived at, in one sentence.",
    )
    largest_extent_m: float | None = Field(
        default=None,
        description="Longest dimension of the model in metres, once scaled.",
    )
    nose_direction: str = Field(
        default="+X",
        description="Axis the nose points along in CAD coordinates.",
    )
    selected_at: str = Field(
        default="",
        description="UTC timestamp, ISO 8601, of when this file was chosen.",
    )
    source: str = Field(
        default="gui",
        description="Which surface recorded it: 'gui' or 'mcp'.",
    )

    def exists(self) -> bool:
        """True when the recorded file is still on disk."""
        return bool(self.step_file_path) and Path(self.step_file_path).is_file()

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for the MCP tools."""
        payload = self.model_dump(mode="json")
        payload["file_exists"] = self.exists()
        payload["units"] = describe_length_unit(self.scale_to_meters)
        return payload

    def summary(self) -> str:
        """One line describing the loaded file, for a status bar or a log."""
        name = Path(self.step_file_path).name or "(none)"
        units = describe_length_unit(self.scale_to_meters)
        if self.largest_extent_m is None:
            return f"{name}, read as {units}"
        return f"{name}, read as {units} — {self.largest_extent_m:.3g} m across"


def workspace_path(root: Path | str | None = None) -> Path:
    """Location of the active-geometry file."""
    base = Path(root) if root is not None else data_root()
    return base / WORKSPACE_FILENAME


def active_geometry(root: Path | str | None = None) -> ActiveGeometry | None:
    """The CAD file currently loaded, or None when there is not one.

    Never raises. A damaged file is treated as no selection, because the
    only thing lost is a convenience.
    """
    target = workspace_path(root)
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        return ActiveGeometry(**payload)
    except (json.JSONDecodeError, OSError, ValidationError, TypeError):
        return None


def set_active_geometry(
    step_file_path: Path | str,
    *,
    scale_to_meters: float | None = None,
    nose_direction: str | None = None,
    source: str = "gui",
    root: Path | str | None = None,
) -> ActiveGeometry:
    """Record a CAD file as the one being worked on.

    Parameters
    ----------
    step_file_path:
        The file chosen. It does not have to exist yet; a path typed into
        the interface is recorded as typed, and ``exists`` reports on it.
    scale_to_meters:
        Override the unit. When None, the file's own declaration is read
        and sanity-checked against the model's size.
    nose_direction:
        Forward axis, if the caller knows it.
    source:
        'gui' or 'mcp', kept so a confusing hand-off can be traced.

    Returns
    -------
    ActiveGeometry
        What was recorded, including how the scale was decided.
    """
    resolved = Path(step_file_path).expanduser()
    try:
        resolved = resolved.resolve()
    except OSError:  # pragma: no cover - unresolvable path, keep it as typed
        pass

    scale = scale_to_meters if scale_to_meters is not None else 1.0
    confident = scale_to_meters is not None
    reason = "set explicitly" if scale_to_meters is not None else ""
    extent: float | None = None

    if scale_to_meters is None and resolved.is_file():
        try:
            decision = suggest_scale_to_meters(resolved)
        except StepInspectionError:
            decision = None
        if decision is not None:
            scale = decision.scale
            confident = decision.confident
            reason = decision.reason
            extent = decision.extent_m
    elif scale_to_meters is not None and resolved.is_file():
        try:
            measured = suggest_scale_to_meters(resolved).extent
        except StepInspectionError:  # pragma: no cover - unreadable file
            measured = None
        extent = None if measured is None else measured * scale

    record = ActiveGeometry(
        step_file_path=str(resolved),
        scale_to_meters=scale,
        scale_is_confident=confident,
        scale_reason=reason,
        largest_extent_m=extent,
        nose_direction=nose_direction or "+X",
        selected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source=source,
    )
    _write(record, root)
    return record


def clear_active_geometry(root: Path | str | None = None) -> None:
    """Forget the loaded file. Missing is not an error."""
    try:
        workspace_path(root).unlink()
    except OSError:
        pass


def _write(record: ActiveGeometry, root: Path | str | None) -> None:
    """Persist the record atomically, swallowing filesystem trouble."""
    target = workspace_path(root)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)
    except OSError:
        # Losing the hand-off means the operator names the file again. It
        # is not worth failing an import over.
        pass
