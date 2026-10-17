"""Hourly rain over Mandaue, 2011 onward, from Open-Meteo's historical API.

The series is ERA5 reanalysis: about 25 km cells, hourly. It smooths short
bursts, so the 1 h figures it gives understate what a gauge would record;
the 3 h, 6 h and 24 h totals are less affected. A gauge record (PAGASA
Mactan) would replace it.

Fetched once and cached in ``data/rain/`` so results reproduce without a
network and tests never touch one.
"""

from __future__ import annotations

import csv
import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from drain.paths import RAIN_CACHE

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

#: Mandaue City, roughly its centre.
MANDAUE_LAT = 10.33
MANDAUE_LON = 123.94

#: Pinned so a refetch returns the same dataset; "best_match" can change.
MODEL = "era5"

TIMEZONE = "Asia/Manila"

HOUR = timedelta(hours=1)

Hourly = list[tuple[datetime, float]]


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)


def fetch_hourly(
    start: date,
    end: date,
    lat: float = MANDAUE_LAT,
    lon: float = MANDAUE_LON,
    get_json: Callable[[str], Any] = _get_json,
) -> Hourly:
    """Hourly precipitation (mm), local time, a year per request.

    Each value is the rain of the hour *ending* at its timestamp, as
    Open-Meteo defines it. A missing hour is an error rather than a zero:
    a hole read as dry weather would make flood days look drier than they
    were.
    """
    series: Hourly = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(date(chunk_start.year, 12, 31), end)
        query = urllib.parse.urlencode(
            {
                "latitude": lat,
                "longitude": lon,
                "start_date": chunk_start.isoformat(),
                "end_date": chunk_end.isoformat(),
                "hourly": "precipitation",
                "models": MODEL,
                "timezone": TIMEZONE,
            }
        )
        hourly = get_json(f"{ARCHIVE_URL}?{query}")["hourly"]
        for stamp, mm in zip(hourly["time"], hourly["precipitation"], strict=True):
            if mm is None:
                raise ValueError(f"no precipitation value for {stamp}")
            series.append((datetime.fromisoformat(stamp), float(mm)))
        logger.info("Fetched rain %s to %s", chunk_start, chunk_end)
        chunk_start = chunk_end + timedelta(days=1)
    return series


def write_cache(series: Hourly, path: Path = RAIN_CACHE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["time", "precipitation_mm"])
        for stamp, mm in series:
            writer.writerow([stamp.strftime("%Y-%m-%dT%H:%M"), f"{mm:g}"])


def read_cache(path: Path = RAIN_CACHE) -> Hourly:
    with open(path, newline="", encoding="utf-8") as handle:
        return [
            (datetime.fromisoformat(row["time"]), float(row["precipitation_mm"]))
            for row in csv.DictReader(handle)
        ]


@dataclass(frozen=True)
class RainFeatures:
    """One local day's rain, in mm."""

    #: Wettest 1, 3 and 6 consecutive hours ending that day.
    max_1h: float
    max_3h: float
    max_6h: float
    total_24h: float
    #: Rain over the three days before; ``None`` where they aren't in the series.
    antecedent_3d: float | None


#: The features, in report order.
FEATURES = ("max_1h", "max_3h", "max_6h", "total_24h", "antecedent_3d")


def daily_features(series: Hourly) -> dict[date, RainFeatures]:
    """Per-day rain features from a continuous hourly series.

    An hour belongs to the day it starts in: the value stamped 00:00 is the
    last hour of the day before. Only days with all 24 hours get features.
    Rolling windows may reach back into the previous day.
    """
    series = sorted(series)
    for (earlier, _), (later, _) in pairwise(series):
        if later - earlier != HOUR:
            raise ValueError(f"hourly series is not continuous at {earlier} -> {later}")

    prefix = [0.0]
    for _, mm in series:
        prefix.append(prefix[-1] + mm)

    def window_max(indices: list[int], hours: int) -> float:
        # Windows that end at each hour of the day and fit in the series.
        best = max(
            (prefix[i + 1] - prefix[i + 1 - hours] for i in indices if i + 1 >= hours),
            default=0.0,
        )
        return round(best, 1)

    by_day: dict[date, list[int]] = {}
    for index, (stamp, _) in enumerate(series):
        by_day.setdefault((stamp - HOUR).date(), []).append(index)

    # Rounded to the source's 0.1 mm, which also drops the prefix sums' float drift.
    totals = {
        day: round(prefix[idx[-1] + 1] - prefix[idx[0]], 1)
        for day, idx in by_day.items()
        if len(idx) == 24
    }
    features: dict[date, RainFeatures] = {}
    for day, total in totals.items():
        indices = by_day[day]
        before = [totals.get(day - timedelta(days=n)) for n in (1, 2, 3)]
        features[day] = RainFeatures(
            max_1h=window_max(indices, 1),
            max_3h=window_max(indices, 3),
            max_6h=window_max(indices, 6),
            total_24h=total,
            antecedent_3d=None if None in before else round(sum(before), 1),
        )
    return features
