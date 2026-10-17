"""Check the flood-hazard model against reported floods, barangay by barangay.

The first check against data that doesn't come from drAIn's own users: do
the barangays news reports say flood most often (Google Groundsource,
``drain.flood_history``) hold the nodes the baseline SWMM run rates worst?

Per barangay: the number of reported flood days (repeats merged), and the
hazard of its nodes in the shipped baseline results, as their mean score and
as the share rated Medium or High. The two are compared by Spearman rank
correlation, with a bootstrap confidence interval. With about 25 barangays
the interval is wide; it is reported, not hidden, and nothing here is a
pass or fail.

News follows people, as citizen reports do, so the same correlation is also
given with population density partialled out.

Butuanon River overflow dominates the news from the riverside barangays, and
the drainage model can't represent a river. Those barangays are listed
separately, and every correlation is given with and without them.

Usage::

    python -m scripts.validate_against_flood_history [--json]

Reads only local files.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass

import numpy as np
from scipy.stats import rankdata, spearmanr

from app.logging_config import configure_logging
from drain.exposure import load_barangays
from drain.flood_history import barangay_names, load_events, merge_repeats
from drain.flooding import build_flooding_summary
from drain.paths import BASE_OUT, BASE_RPT

#: Barangays named in the news of the four Butuanon River overflow floods
#: (2020-10-04, 2022-09-09, 2024-10-05, 2025-08-15) collected by hand in the
#: Project Drain research note docs/research/deep-dive-news-flood-history.md.
RIVERSIDE = ("Alang-alang", "Casuntingan", "Pakna-an", "Tabok", "Tingub", "Umapad")

#: A barangay needs this many model nodes for its hazard to mean anything.
MIN_NODES = 5

BOOTSTRAP_DRAWS = 2000
SEED = 20261018


@dataclass(frozen=True)
class BarangayRow:
    barangay: str
    flood_days: int
    nodes: int
    mean_hazard: float
    share_medium_high: float
    #: People per km²; None where the boundaries give no figure.
    density: float | None
    riverside: bool


def barangay_rows(
    nodes: dict[str, dict],
    flood_counts: Counter[str],
    densities: dict[str, float | None],
    names: tuple[str, ...],
    min_nodes: int = MIN_NODES,
) -> tuple[list[BarangayRow], dict[str, int]]:
    """One row per barangay with enough nodes, and the node counts of those left out."""
    by_barangay: dict[str, list[dict]] = {}
    for row in nodes.values():
        if row.get("Barangay") in names:
            by_barangay.setdefault(row["Barangay"], []).append(row)

    rows, left_out = [], {}
    for name in names:
        group = by_barangay.get(name, [])
        if len(group) < min_nodes:
            left_out[name] = len(group)
            continue
        scores = [r["Vulnerability_Score"] for r in group]
        medium_high = sum(r["Vulnerability_Category"] in ("Medium", "High") for r in group)
        rows.append(
            BarangayRow(
                barangay=name,
                flood_days=flood_counts.get(name, 0),
                nodes=len(group),
                mean_hazard=round(float(np.mean(scores)), 4),
                share_medium_high=round(medium_high / len(group), 4),
                density=densities.get(name),
                riverside=name in RIVERSIDE,
            )
        )
    return rows, left_out


def spearman(x, y) -> float:
    return float(spearmanr(x, y).statistic)


def partial_spearman(x, y, z) -> float:
    """Spearman correlation of x and y with z's rank partialled out:
    the ranks of x and y are each regressed on z's, and the residuals
    correlated."""
    rx, ry, rz = (rankdata(v) for v in (x, y, z))

    def residual(r):
        slope, intercept = np.polyfit(rz, r, 1)
        return r - (slope * rz + intercept)

    ex, ey = residual(rx), residual(ry)
    if not ex.std() or not ey.std():
        return float("nan")
    return float(np.corrcoef(ex, ey)[0, 1])


def bootstrap_interval(
    statistic, columns: list, draws: int = BOOTSTRAP_DRAWS, seed: int = SEED
) -> tuple[float, float] | None:
    """95% percentile interval of ``statistic(*columns)`` over barangays
    resampled with replacement. Draws where it is undefined (a resample
    with no spread) are skipped."""
    rng = np.random.default_rng(seed)
    arrays = [np.asarray(c, dtype=float) for c in columns]
    n = len(arrays[0])
    values = []
    for _ in range(draws):
        pick = rng.integers(0, n, n)
        with np.errstate(all="ignore"):
            value = statistic(*(a[pick] for a in arrays))
        if not np.isnan(value):
            values.append(value)
    if not values:
        return None
    low, high = np.percentile(values, [2.5, 97.5])
    return round(float(low), 3), round(float(high), 3)


@dataclass(frozen=True)
class Correlation:
    barangays: int
    rho: float
    interval: tuple[float, float] | None
    #: With population density partialled out; barangays without a density are left out.
    barangays_with_density: int
    partial_rho: float
    partial_interval: tuple[float, float] | None


def correlate(rows: list[BarangayRow], measure: str) -> Correlation | None:
    if len(rows) < 3:
        return None
    floods = [r.flood_days for r in rows]
    hazard = [getattr(r, measure) for r in rows]
    known = [r for r in rows if r.density is not None]
    kf = [r.flood_days for r in known]
    kh = [getattr(r, measure) for r in known]
    kd = [r.density for r in known]
    return Correlation(
        barangays=len(rows),
        rho=round(spearman(floods, hazard), 3),
        interval=bootstrap_interval(spearman, [floods, hazard]),
        barangays_with_density=len(known),
        partial_rho=round(partial_spearman(kf, kh, kd), 3),
        partial_interval=bootstrap_interval(partial_spearman, [kf, kh, kd]),
    )


MEASURES = ("mean_hazard", "share_medium_high")


def analyse() -> dict:
    names = barangay_names()
    events = merge_repeats(load_events())
    flood_counts = Counter(e.barangay for e in events)
    densities = {b.name: b.density for b in load_barangays() if b.name in names}
    nodes = build_flooding_summary(BASE_RPT, BASE_OUT)["nodes_dict"]
    rows, left_out = barangay_rows(nodes, flood_counts, densities, names)
    inland = [r for r in rows if not r.riverside]
    return {
        "rows": rows,
        "left_out": {
            name: {"nodes": n, "flood_days": flood_counts.get(name, 0)}
            for name, n in left_out.items()
        },
        "all": {m: correlate(rows, m) for m in MEASURES},
        "without_riverside": {m: correlate(inland, m) for m in MEASURES},
    }


def _as_json(result: dict) -> str:
    def encode(value):
        if isinstance(value, BarangayRow | Correlation):
            return asdict(value)
        raise TypeError(type(value))

    return json.dumps(result, indent=2, default=encode)


def _interval(interval) -> str:
    return "undefined" if interval is None else f"[{interval[0]:+.2f}, {interval[1]:+.2f}]"


def print_report(result: dict) -> None:
    print("Reported flood days (Groundsource, 2011-2025) against baseline hazard, by barangay.")
    print(
        f"\n{'barangay':<14} {'flood days':>10} {'nodes':>6} {'mean hazard':>12} "
        f"{'Med/High':>9} {'density':>8}"
    )
    for r in sorted(result["rows"], key=lambda r: -r.flood_days):
        density = "-" if r.density is None else f"{r.density:,.0f}"
        mark = "  (riverside)" if r.riverside else ""
        print(
            f"{r.barangay:<14} {r.flood_days:>10} {r.nodes:>6} {r.mean_hazard:>12.3f} "
            f"{r.share_medium_high:>9.0%} {density:>8}{mark}"
        )
    if result["left_out"]:
        print(f"\nLeft out (fewer than {MIN_NODES} model nodes):")
        for name, row in result["left_out"].items():
            print(f"  {name}: {row['nodes']} node(s), {row['flood_days']} flood day(s)")

    for label, key in (("All barangays", "all"), ("Without riverside", "without_riverside")):
        print(f"\n{label}: Spearman rho, flood days against hazard (95% bootstrap interval)")
        for measure, c in result[key].items():
            if c is None:
                print(f"  {measure}: too few barangays")
                continue
            print(
                f"  {measure:<18} rho {c.rho:+.2f} {_interval(c.interval)}  (n={c.barangays});"
                f"  density partialled out {c.partial_rho:+.2f} "
                f"{_interval(c.partial_interval)}  (n={c.barangays_with_density})"
            )

    riverside = [r for r in result["rows"] if r.riverside]
    if riverside:
        print("\nRiverside barangays (Butuanon overflow dominates their news):")
        for r in riverside:
            print(f"  {r.barangay}: {r.flood_days} flood days, mean hazard {r.mean_hazard:.3f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print the numbers as JSON.")
    parser.add_argument("--log-level", default="WARNING")
    args = parser.parse_args(argv)
    configure_logging(args.log_level.upper())

    result = analyse()
    if args.json:
        print(_as_json(result))
    else:
        print_report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
