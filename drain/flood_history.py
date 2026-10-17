"""Reported Mandaue floods, from Google's Groundsource dataset.

Groundsource read news articles and turned each mention of a flood into a
dated polygon. The Mandaue subset (``data/flood_events/``) gives a start and
end date, the polygon's area and the barangay its centroid falls in. That is
enough to say which barangays were reported flooded on which day, and no
more: there is no depth, time or cause per record.

Two kinds of record are left out. Polygons of 10 km² or more cover most of
the city (it is about 35 km²), so they can't be pinned to a barangay.
Records whose centroid fell in no barangay polygon have no barangay to
count against.

News also repeats itself: a follow-up story a day or two later shows up as
another record. Records in the same barangay within a few days of each other
are merged into one event.
"""

from __future__ import annotations

import csv
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from drain.exposure import _is_city_outline, load_barangays
from drain.paths import FLOOD_EVENTS

logger = logging.getLogger(__name__)

#: Records at least this large are city-wide reports, not barangay ones. The
#: split the research used: 58 of the 357 records are about 30 km².
BARANGAY_SIZED_KM2 = 10.0

#: What the research subset writes when a centroid is in no barangay.
NO_BARANGAY = "(none)"

#: Records in one barangay this many days apart or fewer are the same flood.
MERGE_WINDOW_DAYS = 3

#: A later event this many days after the last one may still be a follow-up
#: story rather than a new flood. Kept, but flagged for the findings.
FOLLOW_UP_DAYS = 14


@dataclass(frozen=True)
class FloodRecord:
    """One Groundsource record."""

    start: date
    end: date
    area_km2: float
    barangay: str


@dataclass(frozen=True)
class FloodEvent:
    """One reported flood in one barangay, repeats merged."""

    barangay: str
    start: date
    end: date
    records: int
    #: Starts within FOLLOW_UP_DAYS of the barangay's previous event.
    possible_follow_up: bool = False


def _key(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.casefold())


def barangay_names() -> tuple[str, ...]:
    """The areas in ``data/mandaue_population.geojson``, city outline left out."""
    barangays = load_barangays()
    return tuple(sorted({b.name for b in barangays if not _is_city_outline(b, barangays)}))


def normalise_name(name: str, names: tuple[str, ...]) -> str:
    """The canonical spelling of a barangay ("Paknaan" -> "Pakna-an").

    Raises ``ValueError`` for a name that isn't a known barangay: a record
    silently dropped over a spelling would bias every count after it.
    """
    match = {_key(n): n for n in names}.get(_key(name))
    if match is None:
        raise ValueError(f"unknown barangay {name!r}")
    return match


def load_events(
    path: Path = FLOOD_EVENTS,
    max_area_km2: float = BARANGAY_SIZED_KM2,
    names: tuple[str, ...] | None = None,
) -> list[FloodRecord]:
    """Read the barangay-sized records, with barangay names normalised."""
    names = barangay_names() if names is None else names
    if not names:
        raise ValueError("no barangay names to normalise against")
    kept: list[FloodRecord] = []
    dropped: Counter[str] = Counter()
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            area = float(row["area_km2"])
            if area >= max_area_km2:
                dropped[f"{max_area_km2:g} km² or larger"] += 1
                continue
            if row["barangay_of_centroid"] == NO_BARANGAY:
                dropped["in no barangay"] += 1
                continue
            kept.append(
                FloodRecord(
                    start=date.fromisoformat(row["start_date"]),
                    end=date.fromisoformat(row["end_date"]),
                    area_km2=area,
                    barangay=normalise_name(row["barangay_of_centroid"], names),
                )
            )
    logger.info("Kept %d flood records; dropped %s", len(kept), dict(dropped) or "none")
    return kept


def merge_repeats(
    records: list[FloodRecord], window_days: int = MERGE_WINDOW_DAYS
) -> list[FloodEvent]:
    """One event per run of records in a barangay no more than ``window_days`` apart."""
    events: list[FloodEvent] = []
    by_barangay: dict[str, list[FloodRecord]] = {}
    for record in records:
        by_barangay.setdefault(record.barangay, []).append(record)

    for barangay, group in sorted(by_barangay.items()):
        group.sort(key=lambda r: (r.start, r.end))
        current: FloodEvent | None = None
        for record in group:
            if current is not None and (record.start - current.end).days <= window_days:
                current = FloodEvent(
                    barangay=barangay,
                    start=current.start,
                    end=max(current.end, record.end),
                    records=current.records + 1,
                    possible_follow_up=current.possible_follow_up,
                )
                continue
            follow_up = current is not None and (record.start - current.end).days <= FOLLOW_UP_DAYS
            if current is not None:
                events.append(current)
            current = FloodEvent(barangay, record.start, record.end, 1, follow_up)
        if current is not None:
            events.append(current)
    return sorted(events, key=lambda e: (e.start, e.barangay))


def flood_days(events: list[FloodEvent]) -> dict[date, set[str]]:
    """Date -> barangays reported flooded. An event counts on its first day:
    later days of a merged event are the follow-up coverage."""
    days: dict[date, set[str]] = {}
    for event in events:
        days.setdefault(event.start, set()).add(event.barangay)
    return days
