#!/usr/bin/env python3
"""Environment verification and solver installation.

Checks the machine, installs the Python dependencies, and fetches the SU2
Windows binaries into the application data directory.

Downloads are verified against a recorded SHA-256 before anything is
extracted. If no checksum is recorded for a release, the download is refused
and manual instructions are printed instead: silently installing an
unverified binary that will then execute on the operator's machine is not an
acceptable convenience.

    python setup_env.py              # check, install dependencies, fetch SU2
    python setup_env.py --check-only # report without changing anything
    python setup_env.py --no-su2     # dependencies only
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.platform_env import (  # noqa: E402
    data_root,
    find_mpi_launcher,
    find_su2_cfd,
    is_windows,
    probe_environment,
)

# Python packages the platform needs, with the import name to test for.
REQUIRED_PACKAGES: tuple[tuple[str, str], ...] = (
    ("numpy", "numpy"),
    ("scipy", "scipy"),
    ("pydantic>=2", "pydantic"),
    ("gmsh", "gmsh"),
    ("pyvista", "pyvista"),
    ("PySide6", "PySide6"),
    ("pyvistaqt", "pyvistaqt"),
    ("pyqtgraph", "pyqtgraph"),
    ("mcp", "mcp"),
    ("requests", "requests"),
)

# Packages that improve the platform but are not required to run it. Each
# carries the sentence printed when it is absent, so the operator can decide
# whether they want it.
OPTIONAL_PACKAGES: tuple[tuple[str, str, str], ...] = (
    (
        "keyring",
        "keyring",
        "the AI assistant's API key would go in a plain-text file instead of "
        "Windows Credential Manager",
    ),
)


def _importable(module: str) -> bool:
    """True when a module can be imported."""
    try:
        __import__(module)
    except ImportError:
        return False
    return True


MS_MPI_DOWNLOAD = "https://www.microsoft.com/en-us/download/details.aspx?id=105289"
SU2_RELEASES = "https://github.com/su2code/SU2/releases"


@dataclass(frozen=True)
class SU2Release:
    """A verified SU2 binary distribution.

    ``sha256`` must be the checksum of the exact archive at ``url``. A value
    of None marks the release as unverified, and it will not be installed
    automatically.
    """

    version: str
    url: str
    sha256: str | None
    archive_name: str

    @property
    def is_verified(self) -> bool:
        """True when a checksum is recorded for this release."""
        return bool(self.sha256)


# Known Windows builds. Checksums must be filled in from the release page
# before automatic installation is enabled for a version; until then the
# installer prints manual instructions rather than fetching blindly.
KNOWN_WINDOWS_RELEASES: tuple[SU2Release, ...] = (
    SU2Release(
        version="8.1.0",
        url=(
            "https://github.com/su2code/SU2/releases/download/v8.1.0/"
            "SU2-v8.1.0-win64-mpi.zip"
        ),
        sha256=None,
        archive_name="SU2-v8.1.0-win64-mpi.zip",
    ),
)


class SetupError(RuntimeError):
    """Raised when the environment cannot be prepared."""


def _heading(text: str) -> None:
    """Print a section heading."""
    print(f"\n{text}\n{'-' * max(len(text), 46)}")


def _status(label: str, value: str, ok: bool | None = None) -> None:
    """Print an aligned status line."""
    mark = "" if ok is None else ("  [ok]" if ok else "  [!]")
    print(f"  {label:<24}: {value}{mark}")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_platform() -> bool:
    """Report the operating system and whether it is the supported target."""
    _heading("Platform")
    system = platform.system()
    release = platform.release()
    _status("Operating system", f"{system} {release}")
    _status("Architecture", platform.machine())
    _status("Python", platform.python_version(), sys.version_info >= (3, 11))

    if sys.version_info < (3, 11):
        print("\n  Python 3.11 or newer is required.")
        return False

    if not is_windows():
        print(
            "\n  Not running on Windows. The platform targets Windows 11 with\n"
            "  Microsoft MPI; the meshing, analysis and visualisation stack\n"
            "  works here, but SU2 must be provided for this OS separately."
        )
    return True


def check_python_packages(install: bool = True) -> list[str]:
    """Verify the Python dependencies, installing any that are missing.

    Returns
    -------
    list of str
        Packages that are still missing afterwards.
    """
    _heading("Python packages")
    missing: list[str] = []

    for requirement, module in REQUIRED_PACKAGES:
        try:
            __import__(module)
            _status(module, "installed", True)
        except ImportError:
            _status(module, "missing", False)
            missing.append(requirement)

    optional_missing: list[tuple[str, str]] = []
    for requirement, module, consequence in OPTIONAL_PACKAGES:
        present = _importable(module)
        _status(
            f"{module} (optional)",
            "installed" if present else "not installed",
            present,
        )
        if not present:
            optional_missing.append((requirement, consequence))
            print(f"      Without it, {consequence}.")

    if optional_missing and install:
        print(f"\n  Installing {len(optional_missing)} optional package(s) ...")
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--upgrade",
                    *(requirement for requirement, _ in optional_missing),
                ],
                check=True,
            )
        except subprocess.CalledProcessError:
            # Optional by definition: report and carry on rather than failing.
            for requirement, consequence in optional_missing:
                print(f"  Could not install {requirement} — {consequence}.")

    if missing and install:
        print(f"\n  Installing {len(missing)} package(s) ...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", *missing],
                check=True,
            )
        except subprocess.CalledProcessError as error:
            print(f"  Installation failed: {error}")
            return missing

        still_missing = []
        for requirement, module in REQUIRED_PACKAGES:
            if requirement not in missing:
                continue
            try:
                __import__(module)
            except ImportError:
                still_missing.append(requirement)
        return still_missing

    return missing


def check_mpi() -> bool:
    """Look for an MPI launcher and explain how to install one if absent."""
    _heading("Message Passing Interface")
    launcher = find_mpi_launcher()
    if launcher is not None:
        _status("Launcher", str(launcher), True)
        return True

    _status("Launcher", "NOT FOUND", False)
    if is_windows():
        print(
            "\n  Microsoft MPI is required for parallel solves. Install both\n"
            "  msmpisetup.exe (the runtime) and msmpisdk.msi from:\n"
            f"    {MS_MPI_DOWNLOAD}\n"
            "  Then reopen the terminal so PATH is refreshed."
        )
    else:
        print(
            "\n  Install an MPI runtime, for example:\n"
            "    sudo apt install mpich        # Debian/Ubuntu\n"
            "  Serial solves still work; set mpi_ranks to 1."
        )
    return False


def check_su2() -> bool:
    """Report whether the SU2 solver binaries are present."""
    _heading("SU2 solver")
    executable = find_su2_cfd()
    if executable is not None:
        _status("SU2_CFD", str(executable), True)
        return True
    _status("SU2_CFD", "NOT FOUND", False)
    return False


# ---------------------------------------------------------------------------
# SU2 installation
# ---------------------------------------------------------------------------


def sha256_of(path: Path, chunk_size: int = 1 << 20) -> str:
    """SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(url: str, destination: Path, progress: bool = True) -> Path:
    """Download a URL to a file, reporting progress."""
    destination.parent.mkdir(parents=True, exist_ok=True)

    def hook(count: int, block_size: int, total: int) -> None:
        if not progress or total <= 0:
            return
        fraction = min(1.0, count * block_size / total)
        bar = "#" * int(40 * fraction)
        print(f"\r  [{bar:<40}] {fraction * 100:5.1f}%", end="", flush=True)

    try:
        urllib.request.urlretrieve(url, destination, reporthook=hook)
    except Exception as error:
        raise SetupError(f"download failed: {error}") from error
    finally:
        if progress:
            print()
    return destination


def install_su2(
    release: SU2Release | None = None,
    target_directory: Path | None = None,
    allow_unverified: bool = False,
) -> Path:
    """Download, verify and extract an SU2 binary distribution.

    Parameters
    ----------
    release:
        Release to install. Defaults to the newest known Windows build.
    target_directory:
        Installation root. Defaults to ``<data root>/su2``.
    allow_unverified:
        Permit installing a release with no recorded checksum. Off by
        default, deliberately.

    Returns
    -------
    Path
        The directory containing the extracted binaries.

    Raises
    ------
    SetupError
        If the release is unverified and not explicitly allowed, if the
        checksum does not match, or if extraction fails.
    """
    release = release or KNOWN_WINDOWS_RELEASES[0]
    target = Path(target_directory) if target_directory else data_root() / "su2"

    if not release.is_verified and not allow_unverified:
        raise SetupError(
            f"no SHA-256 checksum is recorded for SU2 {release.version}, so it "
            "will not be installed automatically.\n"
            f"Download it yourself from {SU2_RELEASES}, extract it to\n"
            f"    {target}\n"
            "or set the SU2_RUN environment variable to the directory holding "
            "SU2_CFD. Pass --allow-unverified to override this check."
        )

    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(scratch) / release.archive_name
        print(f"  Downloading SU2 {release.version} ...")
        download_file(release.url, archive)

        if release.sha256:
            print("  Verifying checksum ...")
            digest = sha256_of(archive)
            if digest.lower() != release.sha256.lower():
                raise SetupError(
                    "checksum mismatch: the download does not match the "
                    f"recorded digest.\n  expected {release.sha256}\n"
                    f"  actual   {digest}\n"
                    "The file has been discarded. Do not install it."
                )
            print("  Checksum verified.")
        else:
            print("  WARNING: installing an unverified archive at your request.")

        print(f"  Extracting to {target} ...")
        try:
            with zipfile.ZipFile(archive) as bundle:
                _safe_extract(bundle, target)
        except zipfile.BadZipFile as error:
            raise SetupError(f"the downloaded archive is not readable: {error}") from error

    binary_directory = _locate_binaries(target)
    print(f"  Installed into {binary_directory}")
    _print_path_instructions(binary_directory)
    return binary_directory


def _safe_extract(bundle: zipfile.ZipFile, target: Path) -> None:
    """Extract an archive, refusing entries that escape the target directory.

    A crafted archive can otherwise write outside the extraction root through
    ``..`` components or absolute paths.
    """
    target = target.resolve()
    for member in bundle.namelist():
        destination = (target / member).resolve()
        if not destination.is_relative_to(target):
            raise SetupError(
                f"archive entry '{member}' would extract outside the target "
                "directory; refusing to continue"
            )
    bundle.extractall(target)


def _locate_binaries(root: Path) -> Path:
    """Find the directory containing SU2_CFD inside an extracted tree."""
    names = ("SU2_CFD.exe", "SU2_CFD")
    for candidate in [root, *sorted(p for p in root.rglob("*") if p.is_dir())]:
        for name in names:
            if (candidate / name).is_file():
                return candidate
    raise SetupError(
        f"the archive extracted to {root} but no SU2_CFD executable was found "
        "inside it"
    )


def _print_path_instructions(binary_directory: Path) -> None:
    """Explain how to make the binaries discoverable in future sessions."""
    print("\n  Add SU2 to your environment so it is found automatically:")
    if is_windows():
        print(f'    setx SU2_RUN "{binary_directory}"')
        print(f'    setx PATH "%PATH%;{binary_directory}"')
    else:
        print(f'    export SU2_RUN="{binary_directory}"')
        print(f'    export PATH="$PATH:{binary_directory}"')
    print("  Then reopen the terminal.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the setup workflow."""
    parser = argparse.ArgumentParser(
        prog="setup_env.py",
        description="Verify and prepare the AeroThermalStudio environment",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="report status without installing anything",
    )
    parser.add_argument(
        "--no-su2", action="store_true", help="skip the SU2 download"
    )
    parser.add_argument(
        "--allow-unverified",
        action="store_true",
        help="install an SU2 archive that has no recorded checksum",
    )
    args = parser.parse_args(argv)

    print("=" * 52)
    print("  AeroThermalStudio environment setup")
    print("=" * 52)

    if not check_platform():
        return 1

    missing = check_python_packages(install=not args.check_only)
    mpi_ready = check_mpi()
    su2_ready = check_su2()

    if not su2_ready and not args.no_su2 and not args.check_only:
        _heading("Installing SU2")
        try:
            install_su2(allow_unverified=args.allow_unverified)
            su2_ready = find_su2_cfd() is not None
        except SetupError as error:
            print(f"\n  {error}")

    _heading("Summary")
    _status("Data directory", str(data_root()))
    _status("Python packages", "complete" if not missing else f"{len(missing)} missing",
            not missing)
    _status("MPI", "ready" if mpi_ready else "missing", mpi_ready)
    _status("SU2", "ready" if su2_ready else "missing", su2_ready)

    if missing:
        print(f"\n  Still missing: {', '.join(missing)}")

    ready = not missing and su2_ready
    if ready:
        print("\n  Setup complete. Try:  python run_app.py --demo")
    else:
        print(
            "\n  Setup incomplete. Meshing, analysis and visualisation work\n"
            "  without SU2; running a solve needs it installed."
        )
    return 0 if ready else 1


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
