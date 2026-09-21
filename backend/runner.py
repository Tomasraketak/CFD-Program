"""Non-blocking execution of the SU2 solver under MPI.

The platform targets ``msmpiexec.exe -n 10 SU2_CFD.exe config.cfg`` on
Windows, but every layer above this one must stay testable on machines with no
SU2 install and in CI. So process launching sits behind :class:`SolverRunner`,
with :class:`FakeRunner` replaying recorded solver output in its place.

Output is streamed line by line to a callback rather than collected at the
end, which is what lets the GUI plot residuals live and lets the convergence
monitor terminate a run early once the forces have settled.
"""

from __future__ import annotations

import os
import queue
import shlex
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Sequence

from core.platform_env import find_mpi_launcher, find_su2_cfd, is_windows

# How long to wait for a cancelled process to exit before killing it.
_TERMINATE_GRACE_S = 5.0

# Sentinel placed on the output queue when the process has finished.
_STREAM_END = object()

LineCallback = Callable[[str], None]
StopPredicate = Callable[[str], bool]


class SolverNotAvailableError(RuntimeError):
    """Raised when the SU2 or MPI executables cannot be located."""


class SolverFailedError(RuntimeError):
    """Raised when the solver exits with a non-zero status.

    Attributes
    ----------
    return_code:
        Process exit status.
    tail:
        Last lines of solver output, which normally carry the real error.
    """

    def __init__(self, return_code: int, tail: Sequence[str]) -> None:
        self.return_code = return_code
        self.tail = list(tail)
        detail = "\n".join(self.tail[-25:])
        super().__init__(
            f"SU2 exited with status {return_code}\n--- solver output ---\n{detail}"
        )


@dataclass
class RunOutcome:
    """Result of one solver invocation."""

    return_code: int
    lines: list[str]
    wall_time_s: float
    cancelled: bool = False
    stopped_early: bool = False
    timed_out: bool = False
    command: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        """True when the solver finished or was stopped deliberately.

        A deliberately stopped or timed-out run is not a solver failure: the
        output gathered up to that point is still the result the caller asked
        for.
        """
        return self.return_code == 0 or self.stopped_early or self.timed_out

    def tail(self, count: int = 25) -> list[str]:
        """Last ``count`` output lines."""
        return self.lines[-count:]


class SolverRunner(ABC):
    """Launches a solver and streams its output."""

    @abstractmethod
    def run(
        self,
        config_path: Path,
        working_directory: Path,
        ranks: int = 1,
        on_line: LineCallback | None = None,
        should_stop: StopPredicate | None = None,
        timeout_s: float | None = None,
    ) -> RunOutcome:
        """Execute the solver to completion.

        Parameters
        ----------
        config_path:
            Solver configuration file.
        working_directory:
            Directory the solver runs in; outputs land here.
        ranks:
            MPI rank count.
        on_line:
            Called with each output line as it arrives.
        should_stop:
            Called with each line; returning True stops the run early. Used to
            terminate once residuals or forces have converged.
        timeout_s:
            Abort after this many seconds.

        Returns
        -------
        RunOutcome
            Exit status, captured output and timing.
        """

    @abstractmethod
    def cancel(self) -> None:
        """Request that a running solve stop as soon as possible."""

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """True while a solve is in progress."""


class SU2Runner(SolverRunner):
    """Runs the real SU2 binary under MPI as a non-blocking subprocess.

    Parameters
    ----------
    su2_executable, mpi_launcher:
        Explicit paths. Both are discovered from the environment when omitted.
    extra_mpi_args:
        Additional arguments inserted before the executable, e.g. host or
        affinity options.
    """

    def __init__(
        self,
        su2_executable: Path | str | None = None,
        mpi_launcher: Path | str | None = None,
        extra_mpi_args: Sequence[str] = (),
    ) -> None:
        self._su2 = Path(su2_executable) if su2_executable else None
        self._mpi = Path(mpi_launcher) if mpi_launcher else None
        self._extra_mpi_args = list(extra_mpi_args)
        self._process: subprocess.Popen[str] | None = None
        self._cancelled = threading.Event()
        self._lock = threading.Lock()

    def resolve_executables(self, ranks: int) -> tuple[Path, Path | None]:
        """Locate SU2 and, when running in parallel, the MPI launcher.

        Raises
        ------
        SolverNotAvailableError
            If SU2 is missing, or MPI is missing for a multi-rank run.
        """
        su2 = self._su2 or find_su2_cfd()
        if su2 is None:
            raise SolverNotAvailableError(
                "SU2_CFD was not found. Run 'python setup_env.py' to install "
                "it, or set the SU2_RUN environment variable to its directory."
            )
        if ranks <= 1:
            return su2, None

        mpi = self._mpi or find_mpi_launcher()
        if mpi is None:
            raise SolverNotAvailableError(
                f"a {ranks}-rank run needs an MPI launcher. Install Microsoft "
                "MPI (msmpiexec.exe) and ensure it is on PATH, or set "
                "mpi_ranks to 1 for a serial solve."
            )
        return su2, mpi

    def build_command(
        self, config_path: Path, ranks: int
    ) -> list[str]:
        """Assemble the full command line for a run."""
        su2, mpi = self.resolve_executables(ranks)
        if mpi is None:
            return [str(su2), config_path.name]
        return [
            str(mpi),
            "-n",
            str(ranks),
            *self._extra_mpi_args,
            str(su2),
            config_path.name,
        ]

    @property
    def is_running(self) -> bool:
        """True while the subprocess is alive."""
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def cancel(self) -> None:
        """Terminate the running solve, escalating to a kill if needed."""
        self._cancelled.set()
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=_TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - timing dependent
            process.kill()
        except OSError:  # pragma: no cover - process already gone
            pass

    def run(
        self,
        config_path: Path,
        working_directory: Path,
        ranks: int = 1,
        on_line: LineCallback | None = None,
        should_stop: StopPredicate | None = None,
        timeout_s: float | None = None,
    ) -> RunOutcome:
        """Launch SU2 and stream its output until it finishes or is stopped."""
        config_path = Path(config_path)
        working_directory = Path(working_directory)
        working_directory.mkdir(parents=True, exist_ok=True)

        command = self.build_command(config_path, ranks)
        self._cancelled.clear()

        environment = dict(os.environ)
        # Keep each rank single-threaded: SU2 parallelises across MPI ranks,
        # and letting the BLAS layer spawn threads too oversubscribes the CPU.
        environment.setdefault("OMP_NUM_THREADS", "1")

        started = time.perf_counter()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(working_directory),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=environment,
                creationflags=_creation_flags(),
            )
        except OSError as error:
            raise SolverNotAvailableError(
                f"could not launch {shlex.join(command)}: {error}"
            ) from error

        with self._lock:
            self._process = process

        lines: list[str] = []
        stopped_early = False
        timed_out = False

        try:
            for line in _iter_process_output(process):
                lines.append(line)
                if on_line is not None:
                    on_line(line)
                if should_stop is not None and should_stop(line):
                    stopped_early = True
                    break
                if timeout_s is not None and (
                    time.perf_counter() - started > timeout_s
                ):
                    timed_out = True
                    break
        finally:
            # Leaving the loop without killing the process would make the
            # wait() below block until the solver finished on its own, which
            # defeats both early termination and the timeout.
            if stopped_early or timed_out or self._cancelled.is_set():
                self.cancel()
            return_code = process.wait()
            with self._lock:
                self._process = None

        outcome = RunOutcome(
            return_code=return_code,
            lines=lines,
            wall_time_s=time.perf_counter() - started,
            cancelled=self._cancelled.is_set() and not stopped_early and not timed_out,
            stopped_early=stopped_early,
            timed_out=timed_out,
            command=command,
        )
        if not outcome.succeeded and not outcome.cancelled:
            raise SolverFailedError(return_code, outcome.tail())
        return outcome


def _creation_flags() -> int:
    """Windows flag preventing a console window from flashing up.

    The GUI launches solves in the background; without this every run pops a
    console window in front of the user.
    """
    if is_windows():
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _iter_process_output(process: subprocess.Popen[str]) -> Iterator[str]:
    """Yield decoded output lines as the process produces them."""
    stream = process.stdout
    if stream is None:  # pragma: no cover - always piped above
        return
    for raw in stream:
        yield raw.rstrip("\n").rstrip("\r")


class FakeRunner(SolverRunner):
    """Replays recorded solver output instead of launching a process.

    This is what makes the solver stack testable without SU2: the parsing,
    convergence and post-processing layers see exactly the text a real run
    produces.

    Parameters
    ----------
    lines:
        Output lines to replay.
    return_code:
        Exit status to report once the lines are exhausted.
    artifacts:
        Files to create in the working directory before replaying, mapping a
        relative path to its contents. Used to stand in for the CSV and
        ``forces_breakdown.dat`` files SU2 writes.
    delay_s:
        Optional per-line delay, for exercising streaming behaviour.
    """

    def __init__(
        self,
        lines: Sequence[str],
        return_code: int = 0,
        artifacts: dict[str, str] | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.lines = list(lines)
        self.return_code = return_code
        self.artifacts = dict(artifacts or {})
        self.delay_s = delay_s
        self.calls: list[dict[str, object]] = []
        self._cancelled = threading.Event()
        self._running = False

    @property
    def is_running(self) -> bool:
        """True while replaying."""
        return self._running

    def cancel(self) -> None:
        """Stop the replay at the next line."""
        self._cancelled.set()

    def run(
        self,
        config_path: Path,
        working_directory: Path,
        ranks: int = 1,
        on_line: LineCallback | None = None,
        should_stop: StopPredicate | None = None,
        timeout_s: float | None = None,
    ) -> RunOutcome:
        """Replay the recorded output, writing any configured artifacts."""
        working_directory = Path(working_directory)
        working_directory.mkdir(parents=True, exist_ok=True)
        self.calls.append(
            {
                "config_path": str(config_path),
                "working_directory": str(working_directory),
                "ranks": ranks,
            }
        )

        for name, contents in self.artifacts.items():
            target = working_directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")

        self._cancelled.clear()
        self._running = True
        started = time.perf_counter()
        emitted: list[str] = []
        stopped_early = False

        try:
            for line in self.lines:
                if self._cancelled.is_set():
                    break
                emitted.append(line)
                if on_line is not None:
                    on_line(line)
                if should_stop is not None and should_stop(line):
                    stopped_early = True
                    break
                if self.delay_s:
                    time.sleep(self.delay_s)
                if timeout_s is not None and (
                    time.perf_counter() - started > timeout_s
                ):
                    break
        finally:
            self._running = False

        outcome = RunOutcome(
            return_code=self.return_code,
            lines=emitted,
            wall_time_s=time.perf_counter() - started,
            cancelled=self._cancelled.is_set(),
            stopped_early=stopped_early,
            command=["<fake>", str(config_path)],
        )
        if not outcome.succeeded and not outcome.cancelled:
            raise SolverFailedError(self.return_code, outcome.tail())
        return outcome


class BackgroundRun:
    """Runs a solve on a worker thread, exposing output through a queue.

    The GUI needs the solver off the Qt event loop while still receiving lines
    promptly; the MCP server uses the same mechanism to keep tool calls
    responsive.
    """

    def __init__(self, runner: SolverRunner) -> None:
        self._runner = runner
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._outcome: RunOutcome | None = None
        self._error: BaseException | None = None

    def start(self, **run_kwargs) -> None:
        """Begin the solve on a background thread."""
        if self._thread is not None:
            raise RuntimeError("this run has already been started")

        def target() -> None:
            try:
                self._outcome = self._runner.run(
                    on_line=self._queue.put, **run_kwargs
                )
            except BaseException as error:  # noqa: BLE001 - re-raised on join
                self._error = error
            finally:
                self._queue.put(_STREAM_END)

        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def lines(self) -> Iterator[str]:
        """Yield output lines as they arrive, ending when the solve does."""
        while True:
            item = self._queue.get()
            if item is _STREAM_END:
                return
            yield item

    def cancel(self) -> None:
        """Request cancellation of the underlying solve."""
        self._runner.cancel()

    def join(self, timeout: float | None = None) -> RunOutcome:
        """Wait for completion and return the outcome.

        Raises
        ------
        BaseException
            Whatever the solve raised, re-raised on the calling thread.
        """
        if self._thread is None:
            raise RuntimeError("this run has not been started")
        self._thread.join(timeout)
        if self._error is not None:
            raise self._error
        if self._outcome is None:
            raise TimeoutError("solve did not finish within the timeout")
        return self._outcome
