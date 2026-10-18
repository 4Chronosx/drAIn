"""How much rain comes with a reported Mandaue flood?

Joins the Groundsource flood days (``drain.flood_history``) to daily rain
features from the cached Open-Meteo series (``drain.rain_history``) and asks,
for each feature, how well it separates flood days from every other day of
2011-2025:

- AUC: the chance a flood day had more of it than a day without a reported
  flood (0.5 is chance, 1.0 is every time);
- the threshold that best balances hits and false alarms (Youden's J), with
  the hit rate and false-alarm rate at that threshold;
- a plain table: of the days with more than X mm in 3 hours, how many had a
  reported flood.

News dates can be a day late (publication, not event), so every number is
also given with a +/-1 day window: a flood day takes the wettest value of the
three days around it, every other day is treated the same way, and days
within a day of a flood are left out of the comparison.

Per barangay the same, where a barangay has at least 8 flood days; the
rest print "insufficient data" with their count.

The rain is reanalysis (about 25 km cells), not a gauge, so short bursts are
smoothed and 1 h figures understate real intensities. Read 3 h and longer.

Usage::

    python -m scripts.flood_rain_thresholds [--json] [--max-area 10] [--refresh-rain]

Reads only local files unless ``--refresh-rain`` is passed, which refetches
the rain from Open-Meteo and rewrites the cache.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, timedelta

import numpy as np
from scipy.stats import mannwhitneyu

from app.logging_config import configure_logging
from drain.flood_history import (
    BARANGAY_SIZED_KM2,
    flood_days,
    load_events,
    merge_repeats,
)
from drain.paths import FLOOD_EVENTS, RAIN_CACHE
from drain.rain_history import FEATURES, daily_features, fetch_hourly, read_cache, write_cache

#: A barangay needs this many flood days before it gets numbers.
MIN_FLOOD_DAYS = 8

#: Day windows the comparison is run at: the reported day, and +/-1 day.
WINDOWS = (0, 1)

#: 3-hour rain levels for the plain-language table, in mm.
EXCEEDANCE_MM = (10, 20, 30, 40, 60)

#: The span of the cached rain.
RAIN_START = date(2011, 1, 1)
RAIN_END = date(2025, 12, 31)


def auc(flood: list[float], other: list[float]) -> float:
    """Mann-Whitney AUC: the chance a flood day beats another day, ties half."""
    return float(mannwhitneyu(flood, other).statistic) / (len(flood) * len(other))


def youden(flood: list[float], other: list[float]) -> tuple[float, float, float]:
    """The threshold (rain >= t means "flood") with the best hit rate minus
    false-alarm rate, and those two rates. Ties go to the higher threshold,
    which raises fewer false alarms for the same J."""
    pos = np.sort(np.asarray(flood))
    neg = np.sort(np.asarray(other))
    best = (-np.inf, 0.0, 0.0, 0.0)
    for threshold in np.unique(pos)[::-1]:
        hit = (len(pos) - np.searchsorted(pos, threshold, side="left")) / len(pos)
        false_alarm = (len(neg) - np.searchsorted(neg, threshold, side="left")) / len(neg)
        if hit - false_alarm > best[0]:
            best = (hit - false_alarm, float(threshold), float(hit), float(false_alarm))
    return best[1], best[2], best[3]


@dataclass(frozen=True)
class Separation:
    flood_days: int
    other_days: int
    mean_flood: float
    mean_other: float
    auc: float
    threshold: float
    hit_rate: float
    false_alarm_rate: float


def separation(flood: list[float], other: list[float]) -> Separation | None:
    """How well one feature separates flood days from the rest; None if either is empty."""
    if not flood or not other:
        return None
    threshold, hit, false_alarm = youden(flood, other)
    return Separation(
        flood_days=len(flood),
        other_days=len(other),
        mean_flood=round(float(np.mean(flood)), 2),
        mean_other=round(float(np.mean(other)), 2),
        auc=round(auc(flood, other), 3),
        threshold=threshold,
        hit_rate=round(hit, 3),
        false_alarm_rate=round(false_alarm, 3),
    )


def windowed(values: dict[date, float], window: int) -> dict[date, float]:
    """Each day's value replaced by the largest within ``window`` days of it."""
    if window == 0:
        return values
    out = {}
    for day in values:
        near = (values.get(day + timedelta(days=k)) for k in range(-window, window + 1))
        out[day] = max(v for v in near if v is not None)
    return out


def split_days(
    values: dict[date, float], flooded: Iterable[date], window: int
) -> tuple[list[float], list[float]]:
    """Values on flood days, and on days more than ``window`` days from any."""
    flooded = set(flooded)
    near = {d + timedelta(days=k) for d in flooded for k in range(-window, window + 1)}
    spread = windowed(values, window)
    flood = [spread[d] for d in sorted(flooded) if d in spread]
    other = [v for d, v in spread.items() if d not in near]
    return flood, other


def feature_values(features: dict, name: str) -> dict[date, float]:
    """One feature by day, days where it is unknown left out."""
    values = {day: getattr(f, name) for day, f in features.items()}
    return {day: v for day, v in values.items() if v is not None}


def separations(
    features: dict, flooded: Iterable[date], window: int
) -> dict[str, Separation | None]:
    flooded = list(flooded)
    return {
        name: separation(*split_days(feature_values(features, name), flooded, window))
        for name in FEATURES
    }


def exceedance(
    features: dict, flooded: Iterable[date], levels: Iterable[float] = EXCEEDANCE_MM
) -> list[dict]:
    """Of the days with more than X mm in 3 hours, how many had a reported flood."""
    flooded = set(flooded)
    values = feature_values(features, "max_3h")
    rows = []
    for level in levels:
        wet = [d for d, v in values.items() if v > level]
        hits = sum(1 for d in wet if d in flooded)
        rows.append(
            {
                "mm_in_3h": level,
                "days": len(wet),
                "flood_days": hits,
                "share": round(hits / len(wet), 3) if wet else None,
            }
        )
    return rows


def per_barangay(
    features: dict, days: dict[date, set[str]], min_days: int = MIN_FLOOD_DAYS
) -> dict[str, dict]:
    """Separations for each barangay with ``min_days`` flood days; ``None`` for the rest."""
    by_barangay: dict[str, list[date]] = {}
    for day, barangays in days.items():
        for barangay in barangays:
            by_barangay.setdefault(barangay, []).append(day)
    result = {}
    for barangay, flooded in sorted(by_barangay.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        enough = len(flooded) >= min_days
        result[barangay] = {
            "flood_days": len(flooded),
            "windows": (
                {w: separations(features, flooded, w) for w in WINDOWS} if enough else None
            ),
        }
    return result


def record_counts(path, max_area_km2: float) -> dict[str, int]:
    """How the raw records split between kept and dropped."""
    with open(path, newline="", encoding="utf-8") as handle:
        raw = sum(1 for _ in csv.DictReader(handle))
    named = len(load_events(path, max_area_km2=float("inf")))
    kept = len(load_events(path, max_area_km2=max_area_km2))
    return {
        "raw": raw,
        "under_cut": raw - (named - kept),
        "city_sized": named - kept,
        "no_barangay": raw - named,
        "kept": kept,
    }


def analyse(features: dict, max_area_km2: float = BARANGAY_SIZED_KM2) -> dict:
    records = load_events(FLOOD_EVENTS, max_area_km2=max_area_km2)
    events = merge_repeats(records)
    days = flood_days(events)
    in_range = {d: b for d, b in days.items() if d in features}
    city = {w: separations(features, in_range, w) for w in WINDOWS}
    plain = city[0]["total_24h"]
    return {
        "max_area_km2": max_area_km2,
        "records": record_counts(FLOOD_EVENTS, max_area_km2),
        "events": len(events),
        "possible_follow_ups": sum(e.possible_follow_up for e in events),
        "flood_days": len(in_range),
        "rain_days": len(features),
        "city": city,
        "exceedance": exceedance(features, in_range),
        "barangays": per_barangay(features, in_range),
        "sanity": {
            "2025-08-15_is_a_flood_day": date(2025, 8, 15) in in_range,
            "flood_days_rainier_on_average": (
                plain is not None and plain.mean_flood > plain.mean_other
            ),
        },
    }


def _as_json(result: dict) -> str:
    def encode(value):
        if isinstance(value, Separation):
            return asdict(value)
        raise TypeError(type(value))

    return json.dumps(result, indent=2, default=encode)


def _print_separations(rows: dict[str, Separation | None]) -> None:
    print(
        f"  {'feature':<14} {'flood':>6} {'other':>6} {'AUC':>5} {'>= mm':>7} {'hits':>5} {'FA':>5}"
    )
    for name, s in rows.items():
        if s is None:
            print(f"  {name:<14} no data")
            continue
        print(
            f"  {name:<14} {s.mean_flood:>6.1f} {s.mean_other:>6.1f} {s.auc:>5.2f} "
            f"{s.threshold:>7.1f} {s.hit_rate:>5.0%} {s.false_alarm_rate:>5.0%}"
        )


def print_report(result: dict) -> None:
    r = result["records"]
    print(f"Groundsource records: {r['raw']}")
    print(f"  under {result['max_area_km2']:g} km2:       {r['under_cut']}")
    print(f"  city-sized (dropped):  {r['city_sized']}")
    print(f"  in no barangay (dropped): {r['no_barangay']}")
    print(f"  kept:                  {r['kept']}")
    print(f"events after merging repeats: {result['events']}")
    print(f"  possible follow-up stories (4-14 days after one): {result['possible_follow_ups']}")
    print(f"flood days: {result['flood_days']} of {result['rain_days']} days with rain data")

    for window, rows in result["city"].items():
        label = "reported day" if window == 0 else f"+/-{window} day"
        print(f"\nCity-wide, {label} (means in mm; FA = false-alarm rate):")
        _print_separations(rows)

    print("\nOf the days with more than X mm in 3 hours, how many had a reported flood?")
    for row in result["exceedance"]:
        share = "-" if row["share"] is None else f"{row['share']:.0%}"
        print(f"  > {row['mm_in_3h']:>3} mm: {row['flood_days']:>3} of {row['days']:>4} ({share})")

    print(f"\nPer barangay (AUC by feature, reported day; needs {MIN_FLOOD_DAYS}+ flood days):")
    print(f"  {'barangay':<14} {'days':>4}  " + " ".join(f"{n:>13}" for n in FEATURES))
    for barangay, row in result["barangays"].items():
        if row["windows"] is None:
            print(f"  {barangay:<14} {row['flood_days']:>4}  insufficient data")
            continue
        aucs = [row["windows"][0][n] for n in FEATURES]
        cells = " ".join(f"{'-' if s is None else f'{s.auc:.2f}':>13}" for s in aucs)
        print(f"  {barangay:<14} {row['flood_days']:>4}  {cells}")

    sanity = result["sanity"]
    print("\nSanity checks:")
    print(f"  records under the cut (research: about 299): {r['under_cut']}")
    print(f"  2025-08-15 is a flood day: {sanity['2025-08-15_is_a_flood_day']}")
    print(f"  flood days rainier on average: {sanity['flood_days_rainier_on_average']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print the numbers as JSON.")
    parser.add_argument(
        "--max-area",
        type=float,
        default=BARANGAY_SIZED_KM2,
        help="Records this large (km2) or larger count as city-wide and are left out.",
    )
    parser.add_argument(
        "--refresh-rain",
        action="store_true",
        help="Refetch the rain from Open-Meteo and rewrite the cache first.",
    )
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)
    configure_logging(args.log_level.upper())

    if args.refresh_rain:
        write_cache(fetch_hourly(RAIN_START, RAIN_END), RAIN_CACHE)
    result = analyse(daily_features(read_cache(RAIN_CACHE)), args.max_area)
    if args.json:
        print(_as_json(result))
    else:
        print_report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
