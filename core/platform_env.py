"""Discovery of the native toolchain: MS-MPI, SU2 binaries and the data root.

The platform targets native Windows 11, but the code must remain importable
and testable on other systems (CI, developer workstations). Every lookup here
degrades gracefully: it reports what is missing rather than raising at import
time, so the GUI and MCP server can start and tell the operator what to install.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

APP_NAME = "AeroThermalStudio"

# Executable names differ between the Windows MS-MPI build and POSIX builds.
_MPI_EXECUTABLES = ("mpiexec", "msmpiexec")
_SU2_CFD_EXECUTABLE = "SU2_CFD"
_SU2_SOL_EXECUTABLE = "SU2_SOL"

# Conventional install locations searched before falling back to PATH.
_WINDOWS_MPI_HINTS = (
    r"C:\Program Files\Microsoft MPI\Bin",
    r"C:\Program Files (x86)\Microsoft MPI\Bin",
)


def is_windows() -> bool:
    """True when running on Windows."""
    return os.name == "nt"


def _executable_names(stem: str) -> tuple[str, ...]:
    """Candidate filenames for an executable stem on this platform."""
    return (f"{stem}.exe",) if is_windows() else (stem,)


def data_root() -> Path:
    """Root directory for meshes, runs and downloaded binaries.

    Uses ``%LOCALAPPDATA%/AeroThermalStudio`` on Windows and
    ``$XDG_DATA_HOME/AeroThermalStudio`` (or ``~/.local/share/...``) elsewhere.
    The ``ATS_DATA_ROOT`` environment variable overrides both, which the test
    suite relies on.
    """
    override = os.environ.get("ATS_DATA_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if is_windows():
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base).expanduser().resolve() / APP_NAME


def _search_paths(extra: tuple[str, ...] = ()) -> list[Path]:
    """Directories to search for binaries, in priority order."""
    paths: list[Path] = []
    su2_run = os.environ.get("SU2_RUN")
    if su2_run:
        paths.append(Path(su2_run))
    paths.append(data_root() / "su2" / "bin")
    paths.extend(Path(p) for p in extra)
    return paths


def find_executable(stem: str, extra_dirs: tuple[str, ...] = ()) -> Path | None:
    """Locate an executable by stem, searching hint directories then PATH."""
    names = _executable_names(stem)
    for directory in _search_paths(extra_dirs):
        for name in names:
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    return None


def find_mpi_launcher() -> Path | None:
    """Locate the MPI launcher (``msmpiexec.exe`` / ``mpiexec``)."""
    hints = _WINDOWS_MPI_HINTS if is_windows() else ()
    for stem in _MPI_EXECUTABLES:
        found = find_executable(stem, hints)
        if found is not None:
            return found
    return None


def find_su2_cfd() -> Path | None:
    """Locate the ``SU2_CFD`` solver binary."""
    return find_executable(_SU2_CFD_EXECUTABLE)


def find_su2_sol() -> Path | None:
    """Locate the ``SU2_SOL`` solution-export binary."""
    return find_executable(_SU2_SOL_EXECUTABLE)


def _probe_version(executable: Path, args: tuple[str, ...]) -> str | None:
    """Run an executable to capture its version banner, tolerating failure."""
    try:
        completed = subprocess.run(
            [str(executable), *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or "") + (completed.stderr or "")
    for line in output.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def cpu_count() -> int:
    """Number of logical processors available to this process."""
    try:
        return len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except AttributeError:
        return os.cpu_count() or 1


def recommended_mpi_ranks() -> int:
    """Rank count that leaves headroom for the GUI.

    On the 6-core / 12-thread target machine this yields 10, matching the
    specified ``msmpiexec -n 10`` invocation.
    """
    return max(1, cpu_count() - 2)


@dataclass
class EnvironmentReport:
    """Summary of the detected native toolchain."""

    os_name: str
    os_version: str
    is_windows: bool
    python_version: str
    cpu_count: int
    recommended_ranks: int
    data_root: str
    mpi_launcher: str | None = None
    mpi_version: str | None = None
    su2_cfd: str | None = None
    su2_sol: str | None = None
    su2_version: str | None = None
    missing: list[str] = field(default_factory=list)

    @property
    def solver_ready(self) -> bool:
        """True when both an MPI launcher and SU2_CFD were found."""
        return self.mpi_launcher is not None and self.su2_cfd is not None

    def as_dict(self) -> dict[str, object]:
        """Return the report as a JSON-serialisable dictionary."""
        return {
            "os_name": self.os_name,
            "os_version": self.os_version,
            "is_windows": self.is_windows,
            "python_version": self.python_version,
            "cpu_count": self.cpu_count,
            "recommended_ranks": self.recommended_ranks,
            "data_root": self.data_root,
            "mpi_launcher": self.mpi_launcher,
            "mpi_version": self.mpi_version,
            "su2_cfd": self.su2_cfd,
            "su2_sol": self.su2_sol,
            "su2_version": self.su2_version,
            "missing": list(self.missing),
            "solver_ready": self.solver_ready,
        }


def probe_environment(probe_versions: bool = True) -> EnvironmentReport:
    """Inspect the machine and report what the solver stack needs.

    Parameters
    ----------
    probe_versions:
        When True, executes the located binaries with ``--version`` to capture
        their banners. Disabled by the test suite to keep runs hermetic.
    """
    mpi = find_mpi_launcher()
    su2_cfd = find_su2_cfd()
    su2_sol = find_su2_sol()

    missing: list[str] = []
    if mpi is None:
        missing.append(
            "Microsoft MPI (msmpiexec.exe) - install MS-MPI v10.1.3 or newer"
        )
    if su2_cfd is None:
        missing.append("SU2_CFD executable - run 'python setup_env.py' to fetch it")

    report = EnvironmentReport(
        os_name=platform.system(),
        os_version=platform.version(),
        is_windows=is_windows(),
        python_version=platform.python_version(),
        cpu_count=cpu_count(),
        recommended_ranks=recommended_mpi_ranks(),
        data_root=str(data_root()),
        mpi_launcher=str(mpi) if mpi else None,
        su2_cfd=str(su2_cfd) if su2_cfd else None,
        su2_sol=str(su2_sol) if su2_sol else None,
        missing=missing,
    )

    if probe_versions:
        if mpi is not None:
            report.mpi_version = _probe_version(mpi, ("-help",))
        if su2_cfd is not None:
            report.su2_version = _probe_version(su2_cfd, ("--version",))

    return report
