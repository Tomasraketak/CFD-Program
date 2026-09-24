"""Persistent application settings.

Preferences that outlive a session and are not part of any one study: where
files were last opened from, which projects were opened recently, and the
defaults a new project starts with.

Settings are deliberately forgiving. A corrupted or partially-written file
falls back to defaults with a warning rather than preventing the application
from starting, because losing a window size is trivial while being locked out
of the application is not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator

from core.models import StrictModel
from core.platform_env import data_root, recommended_mpi_ranks

SETTINGS_FILENAME = "settings.json"

# How many recently-opened projects the File menu remembers.
MAX_RECENT_PROJECTS = 10


class AppSettings(StrictModel):
    """User preferences, stored alongside the run registry."""

    # -- files -------------------------------------------------------------
    recent_projects: list[str] = Field(
        default_factory=list,
        description="Recently opened project paths, most recent first.",
    )
    last_project_directory: str = Field(
        default="",
        description="Directory the last project was opened from or saved to.",
    )
    last_cad_directory: str = Field(
        default="",
        description="Directory the last STEP file was chosen from.",
    )
    last_export_directory: str = Field(
        default="",
        description="Directory renders and exports were last written to.",
    )

    # -- defaults for new work --------------------------------------------
    default_mpi_ranks: int = Field(
        default_factory=lambda: max(1, recommended_mpi_ranks()),
        ge=1,
        le=64,
        description=(
            "MPI ranks a new run starts with. Defaults to the core count less "
            "two, leaving headroom for the interface."
        ),
    )
    default_colormap: str = Field(
        default="turbo",
        description="Colormap preselected in the viewport and for renders.",
    )
    default_render_resolution: str = Field(
        default="4k",
        description="Resolution preselected for saved images.",
    )
    default_mesh_resolution: str = Field(
        default="medium",
        description="Mesh density preset a new project starts with.",
    )

    # -- interface ---------------------------------------------------------
    window_width: int = Field(
        default=1560, ge=800, le=10000, description="Main window width in pixels."
    )
    window_height: int = Field(
        default=980, ge=600, le=10000, description="Main window height in pixels."
    )
    window_maximised: bool = Field(
        default=False, description="Whether the window was maximised on exit."
    )
    active_tab: int = Field(
        default=0, ge=0, le=8, description="Tab index selected on exit."
    )
    confirm_on_exit: bool = Field(
        default=True,
        description="Ask before closing when a project has unsaved changes.",
    )
    show_environment_warning: bool = Field(
        default=True,
        description="Warn at startup when SU2 or MPI cannot be found.",
    )

    # -- AI assistant ------------------------------------------------------
    ai_model: str = Field(
        default="deepseek/deepseek-chat",
        description=(
            "OpenRouter model id the built-in assistant uses. Any id from "
            "https://openrouter.ai/models is accepted; the interface can list "
            "the live catalogue."
        ),
    )
    ai_confirm_long_tools: bool = Field(
        default=True,
        description=(
            "Ask before the assistant starts meshing, a solve or a sweep. "
            "These occupy the machine for minutes to hours, so confirmation "
            "is on by default."
        ),
    )
    ai_retry_attempts: int = Field(
        default=3,
        ge=1,
        le=6,
        description=(
            "How many times one request to OpenRouter is attempted before "
            "giving up. A dropped connection otherwise throws away whatever "
            "the conversation has already paid for. Set to 1 to never retry: "
            "a completion is not idempotent for billing, so a connection that "
            "drops after the model has generated is paid for twice."
        ),
    )
    solver_max_iterations: int = Field(
        default=1000,
        ge=100,
        le=100_000,
        description=(
            "Iteration limit for solves the assistant runs. A solve that "
            "reaches it without meeting the residual criterion is taken as "
            "the result, with a note saying so."
        ),
    )
    ai_max_tool_rounds: int = Field(
        default=12,
        ge=1,
        le=50,
        description=(
            "How many tool-calling rounds one request may take. Caps what a "
            "confused model can spend before it stops."
        ),
    )

    # -- behaviour ---------------------------------------------------------
    autosave_projects: bool = Field(
        default=True,
        description="Save the open project automatically before a solve starts.",
    )
    live_thermal_preview: bool = Field(
        default=True,
        description=(
            "Re-solve the analytical sensor model as sliders move. Cheap "
            "enough to leave on; turn it off on a very slow machine."
        ),
    )

    @field_validator("recent_projects")
    @classmethod
    def _trim_recent(cls, value: list[str]) -> list[str]:
        """Keep the recent list bounded and free of duplicates."""
        seen: list[str] = []
        for item in value:
            if item and item not in seen:
                seen.append(item)
        return seen[:MAX_RECENT_PROJECTS]

    # -- persistence -------------------------------------------------------

    @classmethod
    def path(cls, root: Path | str | None = None) -> Path:
        """Location of the settings file."""
        base = Path(root) if root is not None else data_root()
        return base / SETTINGS_FILENAME

    @classmethod
    def load(cls, root: Path | str | None = None) -> "AppSettings":
        """Read settings, falling back to defaults on any problem.

        Never raises: an unreadable settings file must not stop the
        application from starting.
        """
        target = cls.path(root)
        if not target.is_file():
            return cls()
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return cls()
            return cls(**payload)
        except (json.JSONDecodeError, OSError, ValidationError, TypeError):
            # A damaged settings file is replaced by defaults on the next save.
            return cls()

    def save(self, root: Path | str | None = None) -> Path:
        """Write settings to disk atomically.

        Returns the path written. Failures are swallowed for the same reason
        loading is forgiving, and the path is still returned so a caller can
        report it.
        """
        target = self.path(root)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(self.model_dump_json(indent=2), encoding="utf-8")
            temporary.replace(target)
        except OSError:
            pass
        return target

    # -- convenience -------------------------------------------------------

    def remember_project(self, path: Path | str) -> None:
        """Move a project to the front of the recent list."""
        resolved = str(Path(path).resolve())
        recent = [item for item in self.recent_projects if item != resolved]
        recent.insert(0, resolved)
        self.recent_projects = recent[:MAX_RECENT_PROJECTS]
        self.last_project_directory = str(Path(resolved).parent)

    def forget_project(self, path: Path | str) -> None:
        """Remove a project from the recent list."""
        resolved = str(Path(path).resolve())
        self.recent_projects = [
            item for item in self.recent_projects if item != resolved
        ]

    def existing_recent_projects(self) -> list[str]:
        """Recent projects that are still on disk.

        The menu should not offer a file that has been moved or deleted, but
        the entry is kept in the stored list in case the drive is simply not
        mounted right now.
        """
        return [item for item in self.recent_projects if Path(item).is_file()]

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable form, for the MCP settings tool."""
        return self.model_dump(mode="json")


_cached: AppSettings | None = None


def load_settings(root: Path | str | None = None, refresh: bool = False) -> AppSettings:
    """Process-wide settings, loaded once and cached.

    Parameters
    ----------
    root:
        Override the data directory, used by the test suite.
    refresh:
        Re-read from disk even if settings are already cached.
    """
    global _cached
    if _cached is None or refresh or root is not None:
        _cached = AppSettings.load(root)
    return _cached


def save_settings(
    settings: AppSettings, root: Path | str | None = None
) -> Path:
    """Persist settings and update the cache."""
    global _cached
    _cached = settings
    return settings.save(root)


def reset_settings_cache() -> None:
    """Drop the cached settings, so the next load re-reads from disk."""
    global _cached
    _cached = None
