"""Generic design exploration: DoE, response surfaces and Monte Carlo.

The same machinery DesignXplorer offers, for any number of inputs and any
number of outputs:

* :func:`central_composite` / :func:`latin_hypercube` -- design points in
  coded coordinates ``[-1, 1]^n``;
* :class:`Axis` -- maps a physical input (linear or log scaled) to and from
  the coded axis, and draws Monte Carlo samples from its distribution;
* :class:`Surface` -- a full quadratic polynomial or a thin-plate radial
  basis interpolant through the solved points, with its leave-one-out error.

The radiation-shield study predates this module and keeps its own
three-input version; the SPS30 housing study is built on this one.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np


class ExplorationError(RuntimeError):
    """The design could not be built, fitted or sampled."""


@dataclass(frozen=True)
class Axis:
    """One input: its range, scale and Monte Carlo distribution."""

    name: str
    minimum: float
    maximum: float
    log: bool = False
    distribution: str = "uniform"  # uniform | normal | triangular
    mean: float | None = None
    std: float | None = None
    mode: float | None = None

    def _to(self, values: np.ndarray) -> np.ndarray:
        return np.log(values) if self.log else values

    def _from(self, values: np.ndarray) -> np.ndarray:
        return np.exp(values) if self.log else values

    def encode(self, values: np.ndarray) -> np.ndarray:
        low, high = self._to(np.array(self.minimum)), self._to(np.array(self.maximum))
        return 2.0 * (self._to(np.asarray(values, dtype=float)) - low) / (high - low) - 1.0

    def decode(self, coded: np.ndarray) -> np.ndarray:
        low, high = self._to(np.array(self.minimum)), self._to(np.array(self.maximum))
        return self._from(low + 0.5 * (np.asarray(coded, dtype=float) + 1.0) * (high - low))

    def sample(self, count: int, rng: np.random.Generator) -> np.ndarray:
        low, high = self.minimum, self.maximum
        middle = 0.5 * (low + high)
        if self.distribution == "uniform":
            if self.log:
                # Uniform in log: every decade equally likely (droplet sizes).
                return np.exp(rng.uniform(math.log(low), math.log(high), count))
            return rng.uniform(low, high, count)
        if self.distribution == "triangular":
            return rng.triangular(low, self.mode if self.mode is not None else middle, high, count)
        from scipy.stats import truncnorm

        mean = self.mean if self.mean is not None else middle
        std = self.std if self.std is not None else (high - low) / 6.0
        return truncnorm.rvs(
            (low - mean) / std, (high - mean) / std, loc=mean, scale=std,
            size=count, random_state=rng,
        )


def central_composite(dimensions: int) -> np.ndarray:
    """Face-centred CCD: centre, 2n face points, 2^n corners."""
    faces = []
    for axis in range(dimensions):
        for sign in (-1.0, 1.0):
            point = np.zeros(dimensions)
            point[axis] = sign
            faces.append(point)
    corners = [list(c) for c in itertools.product((-1.0, 1.0), repeat=dimensions)]
    return np.array([np.zeros(dimensions), *faces, *corners])


def latin_hypercube(dimensions: int, points: int, seed: int) -> np.ndarray:
    """Optimised Latin hypercube in coded coordinates, plus the centre."""
    from scipy.stats import qmc

    sampler = qmc.LatinHypercube(d=dimensions, optimization="random-cd", seed=seed)
    return np.vstack([np.zeros(dimensions), 2.0 * sampler.random(points) - 1.0])


def encode(axes: Sequence[Axis], physical: np.ndarray) -> np.ndarray:
    physical = np.atleast_2d(np.asarray(physical, dtype=float))
    return np.column_stack([axis.encode(physical[:, i]) for i, axis in enumerate(axes)])


def decode(axes: Sequence[Axis], coded: np.ndarray) -> np.ndarray:
    coded = np.atleast_2d(np.asarray(coded, dtype=float))
    return np.column_stack([axis.decode(coded[:, i]) for i, axis in enumerate(axes)])


def _quadratic(coded: np.ndarray) -> np.ndarray:
    n = coded.shape[1]
    columns = [np.ones(len(coded))]
    columns += [coded[:, i] for i in range(n)]
    columns += [coded[:, i] ** 2 for i in range(n)]
    columns += [coded[:, i] * coded[:, j] for i in range(n) for j in range(i + 1, n)]
    return np.column_stack(columns)


class Surface:
    """A response surface for one output over coded inputs."""

    def __init__(self, coded: np.ndarray, values: np.ndarray, kind: str = "quadratic") -> None:
        self.x = np.atleast_2d(np.asarray(coded, dtype=float))
        self.y = np.asarray(values, dtype=float)
        self.kind = kind
        terms = _quadratic(self.x[:1]).shape[1]
        minimum = terms if kind == "quadratic" else self.x.shape[1] + 2
        if len(self.y) < minimum:
            raise ExplorationError(
                f"a {kind} response surface over {self.x.shape[1]} inputs needs at "
                f"least {minimum} solved points; {len(self.y)} are solved"
            )
        if kind == "quadratic":
            self.coefficients, *_ = np.linalg.lstsq(_quadratic(self.x), self.y, rcond=None)
            self._rbf = None
        else:
            from scipy.interpolate import RBFInterpolator

            self._rbf = RBFInterpolator(self.x, self.y, kernel="thin_plate_spline", degree=1)

    def predict(self, coded: np.ndarray) -> np.ndarray:
        coded = np.atleast_2d(coded)
        if self._rbf is not None:
            return self._rbf(coded)
        return _quadratic(coded) @ self.coefficients

    def r_squared(self) -> float:
        total = float(np.sum((self.y - self.y.mean()) ** 2))
        if total <= 0.0:
            return 1.0
        return 1.0 - float(np.sum((self.y - self.predict(self.x)) ** 2)) / total

    def leave_one_out(self) -> float:
        """RMS error predicting each point from a surface fitted without it."""
        if self.kind == "quadratic":
            features = _quadratic(self.x)
            if len(self.y) <= features.shape[1]:
                return math.nan
            leverage = np.clip(np.diag(features @ np.linalg.pinv(features)), 0.0, 1.0 - 1e-9)
            press = (self.y - features @ self.coefficients) / (1.0 - leverage)
            return float(np.sqrt(np.mean(press**2)))
        from scipy.interpolate import RBFInterpolator

        errors = []
        for index in range(len(self.y)):
            keep = np.arange(len(self.y)) != index
            try:
                model = RBFInterpolator(self.x[keep], self.y[keep], kernel="thin_plate_spline", degree=1)
            except Exception:  # noqa: BLE001 - too few points left
                return math.nan
            errors.append(float(model(self.x[index : index + 1])[0] - self.y[index]))
        return float(np.sqrt(np.mean(np.square(errors))))
