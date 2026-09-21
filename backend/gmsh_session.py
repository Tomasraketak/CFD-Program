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


def _initialise() -> None:
    """Start Gmsh in a way that survives being called off the main thread.

    ``gmsh.initialize`` installs a SIGINT handler by default, and Python
    refuses to set a signal handler from anything but the main thread of the
    main interpreter. Meshing is exactly the kind of work that runs on a
    worker -- the GUI does it, and so does the AI assistant -- so the default
    turns the first meshing request of a session into ``ValueError: signal
    only works in main thread of the main interpreter``. The library is
    left initialised when that happens, which is why the *second* attempt
    appears to work and the bug reads as a transient one.

    Ctrl+C interruption of the mesher is no loss here: nothing in this
    application runs Gmsh interactively from a terminal.
    """
    try:
        gmsh.initialize(interruptible=False)
    except TypeError:
        # Older Gmsh builds have no interruptible flag. On the main thread
        # the default is harmless; off it, this raises and the caller sees
        # a real error rather than a half-initialised library.
        gmsh.initialize()


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
            _initialise()
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
