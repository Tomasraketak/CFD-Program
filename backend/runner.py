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
import signal
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

# How often the run loop wakes to check its deadlines. The solver's own output
# does not drive this: a wedged MPI rank holding the stdout pipe open produces
# no lines at all, which is exactly the case the deadlines exist to catch.
_POLL_INTERVAL_S = 0.25

# Longest we wait for taskkill itself before giving up on it.
_TREE_KILL_TIMEOUT_S = 10.0

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


class SolverTimeoutError(RuntimeError):
    """Raised when a solve is abandoned for exceeding one of its deadlines.

    Attributes
    ----------
    limit:
        Which deadline was hit -- ``"stall"`` (no output for too long) or
        ``"wall"`` (the run took too long overall).
    limit_s:
        The value of that limit, in seconds.
    elapsed_s:
        How long the run had been going when it was abandoned.
    tail:
        Last lines of solver output, so the caller can see where it stopped.
    """

    def __init__(
        self,
        limit: str,
        limit_s: float,
        elapsed_s: float,
        tail: Sequence[str],
    ) -> None:
        self.limit = limit
        self.limit_s = limit_s
        self.elapsed_s = elapsed_s
        self.tail = list(tail)
        if limit == "stall":
            what = (
                f"produced no output for {limit_s:.0f} s "
                "(the solver is wedged, not merely slow)"
            )
        else:
            what = f"ran for {elapsed_s:.0f} s, past the {limit_s:.0f} s limit"
        detail = "\n".join(self.tail[-25:]) or "(no output at all)"
        super().__init__(
            f"SU2 was stopped because it {what}.\n"
            f"--- last solver output ---\n{detail}"
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
    stalled: bool = False
    command: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        """True when the solver finished or was stopped deliberately.

        ``stopped_early`` means the convergence monitor decided the answer was
        in and killed the process, which is a genuine success. A run we
        abandoned on a deadline is not: reporting a timed-out run as good is
        how a fifteen-minute hang came to look like a finished solve.
        """
        return self.return_code == 0 or self.stopped_early

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
        stall_timeout_s: float | None = None,
        wall_time_limit_s: float | None = None,
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
            Deprecated alias for ``wall_time_limit_s``.
        stall_timeout_s:
            Abandon the run after this many seconds with no output at all. A
            solver that has stopped talking has stopped working.
        wall_time_limit_s:
            Abandon the run after this many seconds in total, however chatty
            it has been.

        Returns
        -------
        RunOutcome
            Exit status, captured output and timing.

        Raises
        ------
        SolverTimeoutError
            If either deadline is reached.
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
        """Terminate the running solve and every process it spawned.

        Killing the launcher alone is not enough. Under MS-MPI the ranks are
        separate processes that survive ``mpiexec`` dying, and a surviving rank
        still holds the stdout pipe it inherited -- which is precisely what
        wedged a read for fifteen minutes. So the whole tree goes.
        """
        self._cancelled.set()
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        _kill_process_tree(process)

    def run(
        self,
        config_path: Path,
        working_directory: Path,
        ranks: int = 1,
        on_line: LineCallback | None = None,
        should_stop: StopPredicate | None = None,
        timeout_s: float | None = None,
        stall_timeout_s: float | None = None,
        wall_time_limit_s: float | None = None,
    ) -> RunOutcome:
        """Launch SU2 and stream its output until it finishes or is stopped."""
        config_path = Path(config_path)
        working_directory = Path(working_directory)
        working_directory.mkdir(parents=True, exist_ok=True)

        if wall_time_limit_s is None:
            wall_time_limit_s = timeout_s

        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError(
                    "this runner is already running a solve; cancel it before "
                    "starting another, or use a separate runner. Starting a "
                    "second solve here would orphan the first."
                )

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
                **_popen_session_kwargs(),
            )
        except OSError as error:
            raise SolverNotAvailableError(
                f"could not launch {shlex.join(command)}: {error}"
            ) from error

        with self._lock:
            self._process = process
        _register_runner(self)

        lines: list[str] = []
        stopped_early = False
        timed_out = False
        stalled = False

        # The reader is a daemon thread so that a pipe read blocked forever --
        # the exact failure this replaces -- can never keep the application
        # alive. The main loop below polls the queue, so its deadlines are
        # evaluated whether or not the solver has said anything.
        output: queue.Queue = queue.Queue()
        reader = threading.Thread(
            target=_drain_output, args=(process, output), daemon=True
        )
        reader.start()

        last_output_at = started
        try:
            while True:
                try:
                    item = output.get(timeout=_POLL_INTERVAL_S)
                except queue.Empty:
                    item = None
                else:
                    if item is _STREAM_END:
                        break

                if item is not None:
                    line = str(item)
                    last_output_at = time.perf_counter()
                    lines.append(line)
                    if on_line is not None:
                        on_line(line)
                    if should_stop is not None and should_stop(line):
                        stopped_early = True
                        break

                # Deadlines are checked on every pass, not only on a silent
                # one: a solver that talks endlessly without converging must
                # still hit the wall-clock cap.
                if self._cancelled.is_set():
                    break
                now = time.perf_counter()
                if wall_time_limit_s is not None and now - started > wall_time_limit_s:
                    timed_out = True
                    break
                if (
                    stall_timeout_s is not None
                    and now - last_output_at > stall_timeout_s
                ):
                    stalled = True
                    break
        finally:
            # Leaving the loop without killing the tree would make the wait
            # below block until the solver finished on its own, which defeats
            # early termination and both deadlines alike.
            if stopped_early or timed_out or stalled or self._cancelled.is_set():
                self.cancel()
            return_code = _wait_bounded(process)
            with self._lock:
                self._process = None
            _unregister_runner(self)

        elapsed = time.perf_counter() - started
        outcome = RunOutcome(
            return_code=return_code,
            lines=lines,
            wall_time_s=elapsed,
            cancelled=(
                self._cancelled.is_set()
                and not stopped_early
                and not timed_out
                and not stalled
            ),
            stopped_early=stopped_early,
            timed_out=timed_out,
            stalled=stalled,
            command=command,
        )
        if stalled:
            raise SolverTimeoutError(
                "stall", float(stall_timeout_s or 0.0), elapsed, outcome.tail()
            )
        if timed_out:
            raise SolverTimeoutError(
                "wall", float(wall_time_limit_s or 0.0), elapsed, outcome.tail()
            )
        if not outcome.succeeded and not outcome.cancelled:
            raise SolverFailedError(return_code, outcome.tail())
        return outcome


def _creation_flags() -> int:
    """Windows creation flags for a solver subprocess.

    ``CREATE_NO_WINDOW`` keeps a console from flashing up in front of the user
    on every background solve. ``CREATE_NEW_PROCESS_GROUP`` is what makes the
    MPI ranks killable as a group afterwards.
    """
    if is_windows():
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    return 0


def _popen_session_kwargs() -> dict[str, object]:
    """Spawn options that make the child's descendants killable together."""
    if is_windows():
        return {}
    # A new session gives the child its own process group, so killpg reaches
    # the MPI ranks without touching this process.
    return {"start_new_session": True}


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    """End ``process`` and everything it spawned, escalating as needed."""
    pid = process.pid
    if is_windows():
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=_TREE_KILL_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover - platform
            pass
        _wait_bounded(process)
        return

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (OSError, ProcessLookupError):
            # Either it is already gone, or it was never given its own group.
            try:
                process.kill()
            except OSError:  # pragma: no cover - process already reaped
                pass
        try:
            process.wait(timeout=_TERMINATE_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            continue


def _wait_bounded(process: subprocess.Popen[str]) -> int:
    """Reap ``process``, killing it rather than waiting indefinitely.

    An unbounded ``wait()`` after a kill is the second way this module used to
    hang: if anything in the tree outlived the signal, the caller blocked here
    instead of in the pipe read.
    """
    try:
        return process.wait(timeout=_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        pass
    try:
        process.kill()
    except OSError:  # pragma: no cover - process already gone
        pass
    try:
        return process.wait(timeout=_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
        # Nothing further we can do without blocking the caller forever, which
        # is the failure this whole change exists to prevent.
        return process.returncode if process.returncode is not None else -1


def _drain_output(process: subprocess.Popen[str], sink: queue.Queue) -> None:
    """Read the process's output into ``sink`` until the pipe closes.

    Runs on a daemon thread: this read is the one that can block forever when
    a killed MPI job leaves a rank holding the pipe.
    """
    stream = process.stdout
    try:
        if stream is not None:
            for raw in stream:
                sink.put(raw.rstrip("\n").rstrip("\r"))
    except (OSError, ValueError):  # pragma: no cover - pipe closed under us
        pass
    finally:
        sink.put(_STREAM_END)


_live_runners: set[SolverRunner] = set()
_registry_lock = threading.Lock()


def _register_runner(runner: SolverRunner) -> None:
    with _registry_lock:
        _live_runners.add(runner)


def _unregister_runner(runner: SolverRunner) -> None:
    with _registry_lock:
        _live_runners.discard(runner)


def live_runners() -> list[SolverRunner]:
    """Every runner with a solve in progress right now."""
    with _registry_lock:
        return list(_live_runners)


def cancel_all_runs() -> int:
    """Cancel every solve in progress, returning how many were asked to stop.

    This is what lets the GUI's Stop button reach a solve the assistant
    started: the MCP layer builds its own runner, which nothing else holds a
    reference to.
    """
    stopped = 0
    for runner in live_runners():
        try:
            runner.cancel()
        except Exception:  # noqa: BLE001 - one bad runner must not block the rest
            continue
        stopped += 1
    return stopped


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
    scripted:
        Per-invocation overrides, consumed in order: each entry may carry
        ``lines``, ``return_code`` and ``artifacts``, and anything it omits
        falls back to the values above. This is what lets a test script a run
        that diverges and then recovers, which a single ``lines`` list cannot
        express.
    """

    def __init__(
        self,
        lines: Sequence[str] = (),
        return_code: int = 0,
        artifacts: dict[str, str] | None = None,
        delay_s: float = 0.0,
        scripted: Sequence[dict[str, object]] | None = None,
    ) -> None:
        self.lines = list(lines)
        self.return_code = return_code
        self.artifacts = dict(artifacts or {})
        self.delay_s = delay_s
        self.scripted = [dict(step) for step in (scripted or ())]
        self.calls: list[dict[str, object]] = []
        self._cancelled = threading.Event()
        self._running = False

    def _script_for(self, index: int) -> dict[str, object]:
        """The lines, exit status and artifacts for invocation ``index``."""
        step = self.scripted[index] if index < len(self.scripted) else {}
        return {
            "lines": list(step.get("lines", self.lines)),  # type: ignore[arg-type]
            "return_code": int(step.get("return_code", self.return_code)),  # type: ignore[arg-type]
            "artifacts": dict(step.get("artifacts", self.artifacts)),  # type: ignore[arg-type]
        }

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
        stall_timeout_s: float | None = None,
        wall_time_limit_s: float | None = None,
    ) -> RunOutcome:
        """Replay the recorded output, writing any configured artifacts."""
        config_path = Path(config_path)
        working_directory = Path(working_directory)
        working_directory.mkdir(parents=True, exist_ok=True)
        script = self._script_for(len(self.calls))
        self.calls.append(
            {
                "config_path": str(config_path),
                "working_directory": str(working_directory),
                "ranks": ranks,
                # The rescue tests need to see what was actually asked for on
                # each stage, not merely that a stage happened.
                "config_text": (
                    config_path.read_text(encoding="utf-8")
                    if config_path.exists()
                    else ""
                ),
            }
        )

        for name, contents in script["artifacts"].items():  # type: ignore[union-attr]
            target = working_directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")

        self._cancelled.clear()
        self._running = True
        _register_runner(self)
        started = time.perf_counter()
        emitted: list[str] = []
        stopped_early = False
        timed_out = False

        try:
            for line in script["lines"]:  # type: ignore[union-attr]
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
                limit = wall_time_limit_s if wall_time_limit_s is not None else timeout_s
                if limit is not None and time.perf_counter() - started > limit:
                    timed_out = True
                    break
        finally:
            self._running = False
            _unregister_runner(self)

        elapsed = time.perf_counter() - started
        return_code = int(script["return_code"])  # type: ignore[arg-type]
        outcome = RunOutcome(
            return_code=return_code,
            lines=emitted,
            wall_time_s=elapsed,
            cancelled=self._cancelled.is_set(),
            stopped_early=stopped_early,
            timed_out=timed_out,
            command=["<fake>", str(config_path)],
        )
        if timed_out:
            raise SolverTimeoutError(
                "wall",
                float(wall_time_limit_s or timeout_s or 0.0),
                elapsed,
                outcome.tail(),
            )
        if not outcome.succeeded and not outcome.cancelled:
            raise SolverFailedError(return_code, outcome.tail())
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
