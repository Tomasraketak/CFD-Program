"""Safe lifecycle management for the Gmsh global API.

Gmsh exposes a single process-wide model stack, so concurrent use from the GUI
thread and a solver worker would corrupt state. Every entry point into the
meshing code acquires the lock below and guarantees ``gmsh.finalize`` runs even
if meshing raises, which otherwise leaves the library initialised and the next
call failing with a confusing error.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

import gmsh

# Gmsh is not thread-safe; serialise all access through one lock.
_GMSH_LOCK = threading.RLock()

# Verbosity levels: 0 silent, 1 errors, 2 warnings, 5 default chatter.
_DEFAULT_VERBOSITY = 1


@contextmanager
def gmsh_session(
    model_name: str,
    verbosity: int = _DEFAULT_VERBOSITY,
    threads: int | None = None,
) -> Iterator[None]:
    """Initialise Gmsh, add a fresh model, and always tear it down.

    Parameters
    ----------
    model_name:
        Name for the model created inside the session.
    verbosity:
        Gmsh ``General.Terminal``/``General.Verbosity`` level. Defaults to
        errors-only so solver logs stay readable.
    threads:
        Number of threads for the meshing kernels. ``None`` leaves the Gmsh
        default in place.

    Yields
    ------
    None
        The session is active for the duration of the ``with`` block.
    """
    with _GMSH_LOCK:
        already_initialised = gmsh.isInitialized()
        if not already_initialised:
            gmsh.initialize()
        try:
            gmsh.option.setNumber("General.Terminal", 1 if verbosity >= 2 else 0)
            gmsh.option.setNumber("General.Verbosity", verbosity)
            if threads is not None and threads > 0:
                gmsh.option.setNumber("General.NumThreads", threads)
                gmsh.option.setNumber("Mesh.MaxNumThreads3D", threads)
            gmsh.model.add(model_name)
            yield
        finally:
            # Remove the model first so a later session in the same process
            # never inherits entities from this one.
            try:
                gmsh.model.remove()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            if not already_initialised:
                try:
                    gmsh.finalize()
                except Exception:  # pragma: no cover - best-effort cleanup
                    pass


def is_active() -> bool:
    """True when the Gmsh library is currently initialised."""
    return bool(gmsh.isInitialized())
