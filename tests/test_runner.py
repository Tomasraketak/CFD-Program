"""Solver process management.

The real subprocess path is exercised with small shell commands rather than
SU2, so streaming, cancellation and failure handling are genuinely tested on a
machine that has no solver installed.
"""

from __future__ import annotations

import sys
import textwrap
import time
from pathlib import Path

import pytest

from backend.runner import (
    BackgroundRun,
    FakeRunner,
    SolverFailedError,
    SolverNotAvailableError,
    SU2Runner,
)


@pytest.fixture
def script(tmp_path) -> Path:
    """A stand-in 'solver' that prints numbered lines then exits."""
    path = tmp_path / "fake_solver.py"
    path.write_text(
        textwrap.dedent(
            """
            import sys, time
            count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
            status = int(sys.argv[2]) if len(sys.argv) > 2 else 0
            for i in range(count):
                print(f"line {i}", flush=True)
                time.sleep(0.01)
            sys.exit(status)
            """
        )
    )
    return path


class ScriptRunner(SU2Runner):
    """SU2Runner variant that launches a Python script instead of SU2.

    Only command construction is overridden, so the streaming, cancellation
    and exit-status handling under test are the production code paths.
    """

    def __init__(self, script: Path, args: list[str] | None = None) -> None:
        super().__init__()
        self._script = script
        self._args = args or []

    def build_command(self, config_path: Path, ranks: int) -> list[str]:
        return [sys.executable, str(self._script), *self._args]


# ---------------------------------------------------------------------------
# Real subprocess behaviour
# ---------------------------------------------------------------------------


def test_output_is_streamed_line_by_line(tmp_path, script):
    """Lines reach the callback as they are produced."""
    received: list[str] = []
    runner = ScriptRunner(script, ["6"])
    outcome = runner.run(
        config_path=tmp_path / "c.cfg",
        working_directory=tmp_path,
        on_line=received.append,
    )
    assert outcome.return_code == 0
    assert received == [f"line {i}" for i in range(6)]
    assert outcome.lines == received
    assert outcome.wall_time_s > 0.0


def test_should_stop_terminates_the_process_early(tmp_path, script):
    """Returning True from should_stop ends the run without waiting."""
    runner = ScriptRunner(script, ["2000"])
    outcome = runner.run(
        config_path=tmp_path / "c.cfg",
        working_directory=tmp_path,
        should_stop=lambda line: line == "line 3",
    )
    assert outcome.stopped_early
    assert outcome.succeeded
    assert outcome.lines[-1] == "line 3"
    assert len(outcome.lines) == 4


def test_non_zero_exit_raises_with_output_attached(tmp_path, script):
    """A failing solve reports its own output, where the real error lives."""
    runner = ScriptRunner(script, ["3", "2"])
    with pytest.raises(SolverFailedError) as excinfo:
        runner.run(config_path=tmp_path / "c.cfg", working_directory=tmp_path)
    assert excinfo.value.return_code == 2
    assert "line 2" in str(excinfo.value)


def test_cancel_stops_a_running_solve(tmp_path, script):
    """Cancellation returns promptly and is reported as cancelled."""
    runner = ScriptRunner(script, ["100000"])
    background = BackgroundRun(runner)
    background.start(config_path=tmp_path / "c.cfg", working_directory=tmp_path)

    # Wait for output to confirm the process is alive, then cancel.
    stream = background.lines()
    assert next(stream).startswith("line")
    background.cancel()

    outcome = background.join(timeout=30)
    assert outcome.cancelled
    assert not runner.is_running


def test_working_directory_is_created_and_used(tmp_path, script):
    """The solver runs in its own run directory, which is created for it."""
    work = tmp_path / "runs" / "case-1"
    runner = ScriptRunner(script, ["2"])
    runner.run(config_path=tmp_path / "c.cfg", working_directory=work)
    assert work.is_dir()


def test_timeout_ends_the_run(tmp_path, script):
    """A run exceeding its timeout stops rather than hanging."""
    runner = ScriptRunner(script, ["100000"])
    started = time.perf_counter()
    outcome = runner.run(
        config_path=tmp_path / "c.cfg",
        working_directory=tmp_path,
        timeout_s=0.5,
    )
    assert time.perf_counter() - started < 30.0
    assert outcome.lines


# ---------------------------------------------------------------------------
# Command construction and executable discovery
# ---------------------------------------------------------------------------


def test_serial_command_omits_the_mpi_launcher(tmp_path):
    """A single-rank run does not need MPI at all."""
    runner = SU2Runner(su2_executable=tmp_path / "SU2_CFD")
    command = runner.build_command(Path("solver.cfg"), ranks=1)
    assert command[-1] == "solver.cfg"
    assert "-n" not in command


def test_parallel_command_matches_the_specified_invocation(tmp_path):
    """The command is 'mpiexec -n <ranks> SU2_CFD <config>'."""
    runner = SU2Runner(
        su2_executable=tmp_path / "SU2_CFD", mpi_launcher=tmp_path / "msmpiexec"
    )
    command = runner.build_command(Path("solver.cfg"), ranks=10)
    assert command[0].endswith("msmpiexec")
    assert command[1:3] == ["-n", "10"]
    assert command[3].endswith("SU2_CFD")
    assert command[4] == "solver.cfg"


def test_extra_mpi_arguments_are_inserted_before_the_executable(tmp_path):
    """Host and affinity options reach the launcher, not the solver."""
    runner = SU2Runner(
        su2_executable=tmp_path / "SU2_CFD",
        mpi_launcher=tmp_path / "msmpiexec",
        extra_mpi_args=["-affinity"],
    )
    command = runner.build_command(Path("solver.cfg"), ranks=4)
    assert command.index("-affinity") < command.index(str(tmp_path / "SU2_CFD"))


def test_missing_su2_is_reported_with_a_remedy(monkeypatch):
    """The error tells the operator how to install the solver."""
    monkeypatch.setattr("backend.runner.find_su2_cfd", lambda: None)
    with pytest.raises(SolverNotAvailableError, match="setup_env.py"):
        SU2Runner().resolve_executables(ranks=1)


def test_missing_mpi_is_reported_with_a_remedy(monkeypatch, tmp_path):
    """A parallel run without MPI explains both fixes."""
    monkeypatch.setattr("backend.runner.find_mpi_launcher", lambda: None)
    runner = SU2Runner(su2_executable=tmp_path / "SU2_CFD")
    with pytest.raises(SolverNotAvailableError, match="Microsoft MPI"):
        runner.resolve_executables(ranks=10)


def test_unlaunchable_command_is_reported_clearly(tmp_path):
    """A missing binary raises the typed error, not a bare OSError."""
    runner = SU2Runner(su2_executable=tmp_path / "does_not_exist")
    with pytest.raises(SolverNotAvailableError, match="could not launch"):
        runner.run(config_path=tmp_path / "c.cfg", working_directory=tmp_path)


# ---------------------------------------------------------------------------
# FakeRunner, which stands in for SU2 throughout the test suite
# ---------------------------------------------------------------------------


def test_fake_runner_replays_recorded_output(tmp_path):
    """Recorded solver output is delivered exactly as a real run would."""
    runner = FakeRunner(lines=["a", "b", "c"])
    received: list[str] = []
    outcome = runner.run(
        config_path=tmp_path / "c.cfg",
        working_directory=tmp_path,
        on_line=received.append,
    )
    assert received == ["a", "b", "c"]
    assert outcome.succeeded


def test_fake_runner_writes_artifacts(tmp_path):
    """Result files SU2 would write are created for the parser to read."""
    runner = FakeRunner(lines=["x"], artifacts={"forces_breakdown.dat": "Total CD: 0.4"})
    work = tmp_path / "run"
    runner.run(config_path=tmp_path / "c.cfg", working_directory=work)
    assert (work / "forces_breakdown.dat").read_text() == "Total CD: 0.4"


def test_fake_runner_records_how_it_was_called(tmp_path):
    """Tests can assert on the rank count and config path passed through."""
    runner = FakeRunner(lines=["x"])
    runner.run(
        config_path=tmp_path / "solver.cfg",
        working_directory=tmp_path,
        ranks=10,
    )
    assert runner.calls[0]["ranks"] == 10
    assert runner.calls[0]["config_path"] == str(tmp_path / "solver.cfg")


def test_fake_runner_honours_should_stop(tmp_path):
    """Early termination behaves as it does for the real runner."""
    runner = FakeRunner(lines=[f"line {i}" for i in range(100)])
    outcome = runner.run(
        config_path=tmp_path / "c.cfg",
        working_directory=tmp_path,
        should_stop=lambda line: line == "line 5",
    )
    assert outcome.stopped_early
    assert len(outcome.lines) == 6


def test_fake_runner_raises_on_failure(tmp_path):
    """A non-zero status raises, as the real runner does."""
    with pytest.raises(SolverFailedError):
        FakeRunner(lines=["bad"], return_code=3).run(
            config_path=tmp_path / "c.cfg", working_directory=tmp_path
        )


# ---------------------------------------------------------------------------
# Background execution
# ---------------------------------------------------------------------------


def test_background_run_yields_lines_then_finishes(tmp_path):
    """The GUI consumes output while the solve runs on another thread."""
    background = BackgroundRun(FakeRunner(lines=["a", "b"], delay_s=0.01))
    background.start(config_path=tmp_path / "c.cfg", working_directory=tmp_path)
    assert list(background.lines()) == ["a", "b"]
    assert background.join(timeout=10).succeeded


def test_background_run_reraises_errors_on_join(tmp_path):
    """A failure on the worker thread surfaces to the caller."""
    background = BackgroundRun(FakeRunner(lines=["x"], return_code=9))
    background.start(config_path=tmp_path / "c.cfg", working_directory=tmp_path)
    list(background.lines())
    with pytest.raises(SolverFailedError):
        background.join(timeout=10)


def test_background_run_cannot_be_started_twice(tmp_path):
    """Reusing a BackgroundRun is a programming error."""
    background = BackgroundRun(FakeRunner(lines=["x"]))
    background.start(config_path=tmp_path / "c.cfg", working_directory=tmp_path)
    background.join(timeout=10)
    with pytest.raises(RuntimeError, match="already been started"):
        background.start(config_path=tmp_path / "c.cfg", working_directory=tmp_path)


def test_joining_an_unstarted_run_is_an_error():
    """Joining before starting is caught rather than hanging."""
    with pytest.raises(RuntimeError, match="not been started"):
        BackgroundRun(FakeRunner(lines=[])).join()
