"""Parsing of SU2 solver output and result files.

Three things are read here:

* the live screen output, for residual and coefficient history as the solve
  runs (this is what drives the GUI's convergence chart and early
  termination);
* ``forces_breakdown.dat``, for the converged integrated coefficients;
* ``history.csv`` and the surface CSV files, for post-processing.

SU2's screen table is whitespace-aligned with a header row naming each column,
so the columns are located by name rather than position. Column sets differ
between releases and between solver types, and a fixed layout would silently
read the wrong quantity.
"""

from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Screen-output column headings, lower-cased, mapped onto canonical names.
# SU2 has used several spellings across releases; all are accepted.
_COLUMN_ALIASES: dict[str, str] = {
    "inner_iter": "iteration",
    "iter": "iteration",
    "time_iter": "time_iteration",
    "outer_iter": "outer_iteration",
    "rms[rho]": "rms_rho",
    "rms[rhou]": "rms_rho_u",
    "rms[rhov]": "rms_rho_v",
    "rms[rhow]": "rms_rho_w",
    "rms[rhoe]": "rms_rho_e",
    "rms[e]": "rms_rho_e",
    "rms[p]": "rms_pressure",
    "rms[k]": "rms_k",
    "rms[w]": "rms_omega",
    "rms[nu]": "rms_nu",
    "cd": "cd",
    "cl": "cl",
    "csf": "cs",
    "cside": "cs",
    "cmx": "cmx",
    "cmy": "cmy",
    "cmz": "cmz",
    "cfx": "cfx",
    "cfy": "cfy",
    "cfz": "cfz",
    "cefficiency": "efficiency",
    "res[rho]": "rms_rho",
    "linsolres": "linear_residual",
    "time(sec)": "time_s",
}

# A screen row is a sequence of numeric fields separated by whitespace and/or
# the vertical bars SU2 draws between columns.
_NUMERIC = re.compile(
    r"^[+-]?(\d+\.?\d*|\.\d+)([eEdD][+-]?\d+)?$"
)

# Lines announcing a failure that should abort the run rather than be parsed.
_ERROR_MARKERS = (
    "Error in",
    "SU2 encountered",
    "terminate called",
    "Segmentation fault",
    "MPI_ABORT",
)


@dataclass
class IterationRecord:
    """One row of the solver's screen history."""

    iteration: int
    values: dict[str, float] = field(default_factory=dict)

    def get(self, name: str, default: float = math.nan) -> float:
        """Look up a canonical quantity, or ``default`` when absent."""
        return self.values.get(name, default)

    @property
    def rms_rho(self) -> float:
        """log10 of the RMS density residual."""
        return self.get("rms_rho")

    @property
    def cd(self) -> float:
        """Drag coefficient at this iteration."""
        return self.get("cd")

    @property
    def cl(self) -> float:
        """Lift coefficient at this iteration."""
        return self.get("cl")


class SU2OutputParser:
    """Incremental parser for SU2 screen output.

    Fed one line at a time while the solver runs. It locates the header row,
    then converts each subsequent numeric row into an
    :class:`IterationRecord`.
    """

    def __init__(self) -> None:
        self.columns: list[str] = []
        self.records: list[IterationRecord] = []
        self.errors: list[str] = []
        self._raw_headers: list[str] = []

    def feed(self, line: str) -> IterationRecord | None:
        """Consume one output line.

        Returns
        -------
        IterationRecord or None
            The record parsed from this line, when it was a history row.
        """
        stripped = line.strip()
        if not stripped:
            return None

        for marker in _ERROR_MARKERS:
            if marker.lower() in stripped.lower():
                self.errors.append(stripped)
                return None

        fields = _split_row(stripped)
        if not fields:
            return None

        if _looks_like_header(fields):
            self._set_columns(fields)
            return None

        if not self.columns:
            return None
        if not all(_NUMERIC.match(f) for f in fields):
            return None
        if len(fields) != len(self.columns):
            return None

        values: dict[str, float] = {}
        for name, field_text in zip(self.columns, fields):
            value = _to_float(field_text)
            if value is not None:
                values[name] = value

        iteration = int(values.get("iteration", len(self.records)))
        record = IterationRecord(iteration=iteration, values=values)
        self.records.append(record)
        return record

    def _set_columns(self, fields: list[str]) -> None:
        """Record the header, mapping known headings to canonical names."""
        self._raw_headers = list(fields)
        self.columns = [
            _COLUMN_ALIASES.get(f.strip().lower(), f.strip().lower())
            for f in fields
        ]

    def history(self, name: str) -> np.ndarray:
        """All recorded values of one quantity, as an array."""
        return np.array(
            [record.get(name) for record in self.records], dtype=float
        )

    @property
    def iterations(self) -> np.ndarray:
        """Iteration numbers seen so far."""
        return np.array([record.iteration for record in self.records])

    @property
    def last(self) -> IterationRecord | None:
        """Most recent record, if any."""
        return self.records[-1] if self.records else None

    def final_value(self, name: str, default: float = math.nan) -> float:
        """The most recent finite value of a quantity."""
        for record in reversed(self.records):
            value = record.get(name)
            if value is not None and math.isfinite(value):
                return value
        return default


def _split_row(line: str) -> list[str]:
    """Split a screen row into fields, dropping SU2's separators."""
    if set(line) <= set("+-| ="):
        return []
    return [part for part in re.split(r"[|\s]+", line.strip()) if part]


def _looks_like_header(fields: list[str]) -> bool:
    """True when a row names columns rather than carrying values."""
    known = sum(1 for f in fields if f.strip().lower() in _COLUMN_ALIASES)
    return known >= 2


def _to_float(text: str) -> float | None:
    """Parse a float, accepting Fortran's ``D`` exponent marker."""
    try:
        return float(text.replace("D", "E").replace("d", "e"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Convergence monitoring
# ---------------------------------------------------------------------------


class ConvergenceMonitor:
    """Decides when a solve has converged and can be stopped early.

    Two independent criteria, either of which ends the run:

    * the density residual has fallen to the requested threshold;
    * the drag coefficient has stopped changing, judged by the relative
      standard deviation over a trailing window.

    The second matters because a RANS solve often reaches steady forces well
    before the residual target, and the forces are what the operator wants.
    """

    def __init__(
        self,
        residual_threshold: float = -5.0,
        force_window: int = 250,
        force_tolerance: float = 1.0e-4,
        minimum_iterations: int = 50,
    ) -> None:
        self.residual_threshold = residual_threshold
        self.force_window = force_window
        self.force_tolerance = force_tolerance
        self.minimum_iterations = minimum_iterations
        self.reason: str | None = None

    def should_stop(self, parser: SU2OutputParser) -> bool:
        """True once either convergence criterion is met."""
        if len(parser.records) < self.minimum_iterations:
            return False

        residual = parser.final_value("rms_rho")
        if math.isfinite(residual) and residual <= self.residual_threshold:
            self.reason = (
                f"density residual reached {residual:.2f} "
                f"(threshold {self.residual_threshold:.2f})"
            )
            return True

        if self.forces_are_steady(parser):
            return True
        return False

    def forces_are_steady(self, parser: SU2OutputParser) -> bool:
        """True when C_d has been stable across the trailing window."""
        history = parser.history("cd")
        finite = history[np.isfinite(history)]
        if finite.size < self.force_window:
            return False

        window = finite[-self.force_window :]
        mean = float(np.mean(window))
        if abs(mean) < 1.0e-12:
            return False
        relative_spread = float(np.std(window)) / abs(mean)
        if relative_spread <= self.force_tolerance:
            self.reason = (
                f"drag coefficient steady to {relative_spread:.2e} over "
                f"{self.force_window} iterations"
            )
            return True
        return False

    def diverged(self, parser: SU2OutputParser) -> bool:
        """True when the residual has gone non-finite or is climbing away."""
        residual = parser.final_value("rms_rho")
        if not math.isfinite(residual):
            return len(parser.records) > 0
        history = parser.history("rms_rho")
        finite = history[np.isfinite(history)]
        if finite.size < 20:
            return False
        # A residual five orders above its best value is not coming back.
        return bool(finite[-1] > finite.min() + 5.0)


# ---------------------------------------------------------------------------
# Result files
# ---------------------------------------------------------------------------

# forces_breakdown.dat lines look like:
#   Total CL:    0.123456 | Pressure (  85%):   0.104 | Friction (  15%): 0.019
_BREAKDOWN = re.compile(
    r"^\s*Total\s+(?P<name>C[A-Za-z_]+)\s*:\s*(?P<value>[-+0-9.eEdD]+)"
)

# Coefficient names as they appear in forces_breakdown.dat.
_BREAKDOWN_ALIASES = {
    "CL": "cl",
    "CD": "cd",
    "CSF": "cs",
    "CSide": "cs",
    "CMx": "cmx",
    "CMy": "cmy",
    "CMz": "cmz",
    "CFx": "cfx",
    "CFy": "cfy",
    "CFz": "cfz",
    "CEff": "efficiency",
}


def parse_forces_breakdown(path: Path | str) -> dict[str, float]:
    """Read integrated coefficients from ``forces_breakdown.dat``.

    Returns
    -------
    dict
        Canonical coefficient names mapped to values. Missing entries are
        simply absent rather than defaulted, so a caller can tell the
        difference between zero and not reported.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"no forces breakdown at {path}")

    coefficients: dict[str, float] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _BREAKDOWN.match(line)
        if not match:
            continue
        name = match.group("name")
        canonical = _BREAKDOWN_ALIASES.get(name)
        if canonical is None:
            continue
        value = _to_float(match.group("value"))
        if value is not None and canonical not in coefficients:
            # The first "Total" block is the whole body; per-marker blocks
            # follow and must not overwrite it.
            coefficients[canonical] = value
    return coefficients


def parse_history_csv(path: Path | str) -> dict[str, np.ndarray]:
    """Read SU2's ``history.csv`` into arrays keyed by canonical name."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"no history file at {path}")

    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return {}
        names = [
            _COLUMN_ALIASES.get(
                column.strip().strip('"').lower(), column.strip().strip('"').lower()
            )
            for column in header
        ]
        columns: list[list[float]] = [[] for _ in names]
        for row in reader:
            if len(row) != len(names):
                continue
            for index, cell in enumerate(row):
                value = _to_float(cell.strip())
                columns[index].append(math.nan if value is None else value)

    return {
        name: np.array(values, dtype=float)
        for name, values in zip(names, columns)
    }


def parse_surface_csv(path: Path | str) -> dict[str, np.ndarray]:
    """Read a SU2 surface solution CSV (``surface_flow.csv``).

    Column names are lower-cased and stripped of quotes but otherwise left
    alone, since the useful set depends on which outputs were requested.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"no surface solution at {path}")

    with path.open(newline="", encoding="utf-8", errors="replace") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return {}
        names = [column.strip().strip('"').lower() for column in header]
        columns: list[list[float]] = [[] for _ in names]
        for row in reader:
            if len(row) != len(names):
                continue
            for index, cell in enumerate(row):
                value = _to_float(cell.strip())
                columns[index].append(math.nan if value is None else value)

    return {
        name: np.array(values, dtype=float)
        for name, values in zip(names, columns)
    }
