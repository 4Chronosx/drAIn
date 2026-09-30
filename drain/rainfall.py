"""Synthetic design-storm generation.

SWMM needs a rainfall time series, but the API only accepts a total depth and
a duration. These helpers turn that pair into an intensity hyetograph.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

import numpy as np

Pattern = Literal["uniform", "triangular", "chicago"]

DEFAULT_INTERVAL_MIN = 5
DEFAULT_PATTERN: Pattern = "triangular"

#: Fraction of the storm duration at which a "chicago" hyetograph peaks.
CHICAGO_PEAK_POSITION = 0.4


class RainfallStep(NamedTuple):
    """One row of a SWMM ``[TIMESERIES]`` section."""

    hours: float
    intensity_mm_hr: float


def _shape(pattern: Pattern, n: int) -> np.ndarray:
    """Return an unnormalised storm profile of ``n`` samples."""
    if pattern == "uniform":
        return np.ones(n)
    if pattern == "triangular":
        return np.concatenate([np.linspace(0, 1, n // 2), np.linspace(1, 0, n - n // 2)])
    if pattern == "chicago":
        peak = int(n * CHICAGO_PEAK_POSITION)
        return np.concatenate([np.linspace(0, 1, peak), np.linspace(1, 0, n - peak)])
    raise ValueError(f"Unknown rainfall pattern {pattern!r}; expected one of {Pattern.__args__}.")


def generate_rainfall_event(
    total_rain_mm: float,
    duration_hr: float,
    interval_min: int = DEFAULT_INTERVAL_MIN,
    pattern: Pattern = DEFAULT_PATTERN,
) -> list[RainfallStep]:
    """Build a hyetograph totalling ``total_rain_mm`` over ``duration_hr``.

    The returned intensities are in mm/hr, which is what SWMM expects for a
    rain gage in ``INTENSITY`` mode. Depths are distributed according to
    ``pattern`` and then rescaled so the series integrates back to
    ``total_rain_mm``.
    """
    if total_rain_mm < 0:
        raise ValueError("total_rain_mm must not be negative.")
    if duration_hr <= 0:
        raise ValueError("duration_hr must be positive.")
    if interval_min <= 0:
        raise ValueError("interval_min must be positive.")

    step_hr = interval_min / 60
    times = np.arange(0, duration_hr + step_hr, step_hr)
    shape = _shape(pattern, len(times))

    total = shape.sum()
    depths = np.zeros_like(shape) if total == 0 else shape / total * total_rain_mm

    # Depth per interval -> average intensity over that interval.
    intensities = depths * (60 / interval_min)

    return [
        RainfallStep(round(float(t), 2), round(float(i), 2))
        for t, i in zip(times, intensities, strict=True)
    ]
