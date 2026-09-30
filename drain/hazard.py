"""Scoring how badly a node floods.

This replaces a k-means clustering that had three problems. It grouped nodes
rather than ranking them, so 1,413 nodes collapsed to four scores and "which
of these do we fix first" had no answer. It was fitted on flooded nodes but
scored every node, so the sentinel used for "never overflowed" arrived as a
value fifty standard deviations outside anything it had seen. And it treated
a late-starting flood as a mild one, which is how nodes that flooded for
eleven hours came out labelled "No risk".

What replaces it is deliberately plain: three measurements that everyone
agrees are worse when larger, each scaled against a fixed reference, then
averaged. It is monotonic -- more water, or longer, or faster can only raise
the score -- and every number in it can be explained to a city engineer.

The reference values are provisional. They come from the 95th percentile of
the shipped baseline run, not from a damage study, and they are the first
thing that should change once there is field evidence to set them against.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

#: Flood volume, in millions of litres, treated as full scale. The 95th
#: percentile of flooded nodes in the baseline run.
VOLUME_FULL_SCALE: Final = 45.0

#: Peak overflow rate, in cubic metres per second, treated as full scale.
RATE_FULL_SCALE: Final = 1.0

#: How the three measurements are weighted.
#:
#: Volume leads because it is the most direct statement of how much water
#: ended up on the street. Duration follows: a road under water for hours
#: is a different problem from one that drains in minutes. Peak rate carries
#: least weight -- it speaks to how dangerous the moment is rather than how
#: large the event is -- but it is what makes a fast, short flood register.
VOLUME_WEIGHT: Final = 0.5
DURATION_WEIGHT: Final = 0.3
RATE_WEIGHT: Final = 0.2

#: Category boundaries on the 0-1 score.
CATEGORY_THRESHOLDS: Final = ((0.5, "High"), (0.25, "Medium"), (0.0, "Low"))

#: Used for a node with no flooding at all. Phrased to keep the colour
#: mapping the frontend already has, which matches on the words below.
NO_HAZARD = "No hazard"

#: Fallback when the event length is unknown or nonsensical.
DEFAULT_EVENT_HOURS: Final = 24.0


def _clamp_fraction(value: float, full_scale: float) -> float:
    """Scale a measurement to 0-1, with anything past full scale pinned to 1.

    Infinity pins to 1 like any other value past full scale. A NaN scores 0:
    it is a missing figure, and letting it through would make the whole
    score NaN, which is not valid JSON.
    """
    if not (math.isfinite(full_scale) and full_scale > 0) or math.isnan(value):
        return 0.0
    return min(max(value, 0.0) / full_scale, 1.0)


def _known(value: float) -> float:
    """A measurement, with a missing (NaN) one read as nothing."""
    return 0.0 if math.isnan(value) else value


@dataclass(frozen=True)
class Hazard:
    """How badly one node floods."""

    #: 0-1. Zero means the node did not flood.
    score: float
    category: str

    @property
    def floods(self) -> bool:
        return self.score > 0


def hazard_for(
    total_flood_volume: float,
    hours_flooded: float,
    maximum_rate_cms: float,
    event_hours: float = DEFAULT_EVENT_HOURS,
) -> Hazard:
    """Score a node from its flooding, and put it in a category.

    ``event_hours`` is the length of the simulated storm. Duration is scored
    as a share of it, because a one-hour event cannot flood anything for
    twenty hours and should not be penalised against a scale that assumes it
    could.

    Non-finite input never reaches the payload: a NaN measurement counts as
    zero, an infinite one as full scale, and a non-finite event length falls
    back to the default.
    """
    total_flood_volume = _known(total_flood_volume)
    hours_flooded = _known(hours_flooded)
    maximum_rate_cms = _known(maximum_rate_cms)
    if hours_flooded <= 0 and total_flood_volume <= 0 and maximum_rate_cms <= 0:
        return Hazard(score=0.0, category=NO_HAZARD)

    if not math.isfinite(event_hours) or event_hours <= 0:
        event_hours = DEFAULT_EVENT_HOURS

    score = (
        VOLUME_WEIGHT * _clamp_fraction(total_flood_volume, VOLUME_FULL_SCALE)
        + DURATION_WEIGHT * _clamp_fraction(hours_flooded, event_hours)
        + RATE_WEIGHT * _clamp_fraction(maximum_rate_cms, RATE_FULL_SCALE)
    )

    return Hazard(score=round(score, 6), category=categorise(score))


def categorise(score: float) -> str:
    """Bucket a hazard score. Any flooding at all is at least Low.

    A NaN score is not flooding anyone can vouch for, so it is not rated.
    """
    if not score > 0:
        return NO_HAZARD
    for threshold, name in CATEGORY_THRESHOLDS:
        if score > threshold:
            return name
    return "Low"
