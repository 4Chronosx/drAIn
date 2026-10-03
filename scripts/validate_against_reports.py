"""Check the flood-hazard model against citizen reports.

The hazard score is built from simulated flooding alone, and nothing has
ever checked whether it matches the flooding people actually experience.
The reports table is the only ground truth available --
each report is pinned to the nearest drainage component -- so this asks the
obvious question:

    do the components people report flooding at rank higher in the model
    than the ones they do not?

It also compares the model against a baseline of sorting by flood volume
alone, because a model that cannot beat one column is not earning its place.

People report where people are, so a city-wide comparison partly measures
population. The within-barangay comparison controls for that: inside each
barangay, does a reported node have a higher hazard score than an unreported
one? (Hazard, not risk: risk already multiplies in population.) Barangays
with too few reported nodes say "insufficient data" instead of a number.

By default only strong evidence counts: reports staff confirmed, or whose
photo was taken within 100 m of the component. ``--all-reports`` includes
unreviewed ones. ``--min-reporters`` requires that many different people
per component; signed-out reports on one component count as one person,
since they can't be told apart.

Usage::

    SUPABASE_URL=... SUPABASE_KEY=... python -m scripts.validate_against_reports

Use the service-role key to count distinct reporters: signed-out (anon)
keys can't read reports.user_id, and the script then treats every report
as coming from one unknown person per component.

Reads nothing but the reports table, and writes nothing back. Reports
agency staff rejected (spam, duplicates, not a drainage problem) are left
out, as they are from every count in the app.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.logging_config import configure_logging
from drain.flooding import build_flooding_summary
from drain.hazard import CATEGORY_THRESHOLDS, NO_HAZARD
from drain.paths import BASE_OUT, BASE_RPT

logger = logging.getLogger(__name__)

#: Every category the scorer emits, lowest first. Built from drain.hazard so
#: it can't drift: it used to list "No risk", the old k-means label, and a
#: real run died on the first "No hazard" node.
HAZARD_RANK = {
    NO_HAZARD: 0,
    **{name: rank for rank, (_, name) in enumerate(reversed(CATEGORY_THRESHOLDS), start=1)},
}

#: The API returns at most this many rows per request (max_rows), without
#: saying it stopped, so reports are read a page at a time.
PAGE_SIZE = 1000

#: Work-list sizes to report agreement at. An agency acts on the top of the
#: list, so that is where a ranking has to be right.
TOP_N_SIZES = (10, 20, 50, 100)


#: Barangays with fewer reported nodes than this get "insufficient data".
MIN_SAMPLE = 5


@dataclass(frozen=True)
class Report:
    component_id: str
    category: str | None
    created_at: str | None
    review_status: str | None = None
    photo_check: str | None = None
    #: None for a report filed signed out, or when the key can't read it.
    user_id: str | None = None

    @property
    def is_strong_evidence(self) -> bool:
        """Staff confirmed it, or its photo was taken at the component."""
        return self.review_status == "confirmed" or self.photo_check == "match"


def _get_json(url: str, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_reports(
    url: str,
    key: str,
    get_json: Callable[[str, dict[str, str]], Any] = _get_json,
) -> list[Report]:
    """Read every report staff haven't rejected, through PostgREST.

    A page at a time: one request used to ask for 10,000 rows and silently
    got the first 1,000.
    """
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    columns = "component_id,category,created_at,review_status,photo_check"
    try:
        rows = _read_all(url, headers, columns + ",user_id", get_json)
    except urllib.error.HTTPError as error:
        if error.code not in (401, 403):
            raise
        # Signed-out keys can't read who filed a report.
        print("note: this key can't read reports.user_id; reporters can't be told apart.")
        rows = _read_all(url, headers, columns, get_json)

    return [
        Report(
            component_id=str(row["component_id"]),
            category=row.get("category"),
            created_at=row.get("created_at"),
            review_status=row.get("review_status"),
            photo_check=row.get("photo_check"),
            user_id=row.get("user_id"),
        )
        for row in rows
        if row.get("component_id")
    ]


def _read_all(
    url: str,
    headers: dict[str, str],
    columns: str,
    get_json: Callable[[str, dict[str, str]], Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        query = urllib.parse.urlencode(
            {
                "select": columns,
                "review_status": "neq.rejected",
                "order": "id",
                "limit": PAGE_SIZE,
                "offset": offset,
            }
        )
        page = get_json(f"{url.rstrip('/')}/rest/v1/reports?{query}", headers)
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def select_reports(reports: list[Report], *, all_reports: bool, min_reporters: int) -> list[Report]:
    """The reports that count as evidence, on components enough people reported."""
    kept = reports if all_reports else [r for r in reports if r.is_strong_evidence]
    people = reporters_per_component(kept)
    return [r for r in kept if people[r.component_id] >= min_reporters]


def reporters_per_component(reports: list[Report]) -> Counter[str]:
    """Distinct people per component. Each account counts once; all
    signed-out reports on one component together count once."""
    seen = {(r.component_id, r.user_id) for r in reports}
    return Counter(component for component, _ in seen)


def auc(higher: list[float], lower: list[float]) -> float:
    """The chance a value from ``higher`` beats one from ``lower``, ties
    counting half (the Mann-Whitney AUC). 0.5 is no better than chance."""
    wins = sum((h > low) + 0.5 * (h == low) for h in higher for low in lower)
    return wins / (len(higher) * len(lower))


@dataclass(frozen=True)
class BarangayComparison:
    barangay: str
    reported: int
    unreported: int
    #: None when there is too little to compare.
    auc: float | None


def within_barangay(
    matched: set[str], nodes: dict[str, dict], min_sample: int = MIN_SAMPLE
) -> list[BarangayComparison]:
    """Hazard of reported against unreported nodes, one barangay at a time."""
    groups: dict[str, tuple[list[float], list[float]]] = {}
    for node_id, row in nodes.items():
        barangay = row.get("Barangay")
        if not barangay:
            continue
        reported, unreported = groups.setdefault(barangay, ([], []))
        (reported if node_id in matched else unreported).append(row["Vulnerability_Score"])

    comparisons = []
    for barangay, (reported, unreported) in sorted(groups.items()):
        enough = len(reported) >= min_sample and unreported
        comparisons.append(
            BarangayComparison(
                barangay=barangay,
                reported=len(reported),
                unreported=len(unreported),
                auc=auc(reported, unreported) if enough else None,
            )
        )
    return comparisons


def pooled_auc(comparisons: list[BarangayComparison]) -> float | None:
    """The barangays' AUCs averaged, weighted by how many pairs each compared."""
    usable = [c for c in comparisons if c.auc is not None]
    pairs = sum(c.reported * c.unreported for c in usable)
    if not pairs:
        return None
    return sum(c.auc * c.reported * c.unreported for c in usable) / pairs


def print_within_barangay(comparisons: list[BarangayComparison], min_sample: int) -> None:
    print("\nWithin each barangay: does a reported node have higher hazard than an")
    print("unreported one? (AUC: 0.5 is chance, 1.0 is every time.)")
    print(f"{'barangay':<16} {'reported':>8} {'others':>7}  AUC")
    for c in comparisons:
        if c.reported == 0:
            continue
        verdict = f"{c.auc:.2f}" if c.auc is not None else f"insufficient data (< {min_sample})"
        print(f"{c.barangay:<16} {c.reported:>8} {c.unreported:>7}  {verdict}")
    pooled = pooled_auc(comparisons)
    if pooled is None:
        print(f"\npooled: insufficient data (no barangay has {min_sample}+ reported nodes)")
    else:
        print(f"\npooled within-barangay AUC: {pooled:.2f}")


def summarise_join(reports: list[Report], nodes: dict[str, dict]) -> set[str]:
    """Report how many reported components exist in the model, and return them."""
    reported = {report.component_id for report in reports}
    matched = reported & set(nodes)
    unmatched = reported - set(nodes)

    print(f"reports:                     {len(reports)}")
    print(f"distinct components reported: {len(reported)}")
    print(f"  matched to a model node:    {len(matched)}")
    print(f"  not in the model:           {len(unmatched)}")
    if unmatched:
        # Reports pinned to pipes will not match, since the model scores
        # nodes. A high rate here means the join itself needs work before
        # any of the numbers below mean anything.
        print(f"    examples: {sorted(unmatched)[:5]}")
    return matched


def compare_distributions(matched: set[str], nodes: dict[str, dict]) -> None:
    """Contrast the hazard ratings of reported and unreported components."""
    reported_ranks = [HAZARD_RANK[nodes[n]["Vulnerability_Category"]] for n in matched]
    unreported_ranks = [
        HAZARD_RANK[row["Vulnerability_Category"]]
        for node_id, row in nodes.items()
        if node_id not in matched
    ]

    if not reported_ranks:
        print("\nNo reported component matched a model node; nothing to compare.")
        return

    print("\nHazard rating of components people reported:")
    for category, count in sorted(
        Counter(nodes[n]["Vulnerability_Category"] for n in matched).items(),
        key=lambda item: HAZARD_RANK[item[0]],
    ):
        share = 100 * count / len(matched)
        print(f"   {category:9s} {count:5d}  ({share:.0f}%)")

    mean_reported = sum(reported_ranks) / len(reported_ranks)
    mean_unreported = sum(unreported_ranks) / len(unreported_ranks) if unreported_ranks else 0.0
    print(f"\nmean hazard rank, reported components:   {mean_reported:.3f}")
    print(f"mean hazard rank, unreported components: {mean_unreported:.3f}")
    print(
        "  -> the model separates them"
        if mean_reported > mean_unreported
        else "  -> the model does NOT rank reported components higher"
    )


def compare_against_volume_baseline(matched: set[str], nodes: dict[str, dict]) -> None:
    """Does the model find reported components better than one column does?"""
    by_model = sorted(
        nodes.items(),
        key=lambda item: (
            -HAZARD_RANK[item[1]["Vulnerability_Category"]],
            -item[1]["Total_Flood_Volume_10e6_ltr"],
        ),
    )
    by_volume = sorted(nodes.items(), key=lambda item: -item[1]["Total_Flood_Volume_10e6_ltr"])

    print("\nOf the components people reported, how many appear in the top N?")
    print(f"{'N':>6}  {'model':>8}  {'volume only':>12}")
    for n in TOP_N_SIZES:
        model_hits = sum(1 for node_id, _ in by_model[:n] if node_id in matched)
        volume_hits = sum(1 for node_id, _ in by_volume[:n] if node_id in matched)
        print(f"{n:>6}  {model_hits:>8}  {volume_hits:>12}")
    print(
        "\nIf the two columns match, the hazard score is not adding anything a\n"
        "sort on flood volume does not already give you."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supabase-url", default=os.getenv("SUPABASE_URL"))
    parser.add_argument("--supabase-key", default=os.getenv("SUPABASE_KEY"))
    parser.add_argument("--log-level", default="WARNING")
    parser.add_argument(
        "--all-reports",
        action="store_true",
        help="Count unreviewed reports too, not only confirmed ones and photo matches.",
    )
    parser.add_argument(
        "--min-reporters",
        type=int,
        default=1,
        help="Different people needed before a component counts as reported.",
    )
    parser.add_argument(
        "--min-sample",
        type=int,
        default=MIN_SAMPLE,
        help="Reported nodes a barangay needs before it gets an AUC.",
    )
    args = parser.parse_args(argv)

    configure_logging(args.log_level.upper())

    if not args.supabase_url or not args.supabase_key:
        parser.error("Set SUPABASE_URL and SUPABASE_KEY, or pass --supabase-url/--supabase-key.")

    try:
        reports = fetch_reports(args.supabase_url, args.supabase_key)
    except Exception as error:
        print(f"Could not read the reports table: {error}", file=sys.stderr)
        return 1

    evidence = select_reports(
        reports, all_reports=args.all_reports, min_reporters=args.min_reporters
    )
    kind = "all reports" if args.all_reports else "confirmed or photo-matched reports"
    print(f"using {kind}, {args.min_reporters}+ reporter(s) per component")
    print(f"  {len(evidence)} of {len(reports)} reports\n")

    # The baseline network: what the model says with no storm applied.
    nodes = build_flooding_summary(BASE_RPT, BASE_OUT)["nodes_dict"]

    matched = summarise_join(evidence, nodes)
    if len(matched) < args.min_sample:
        print(
            f"\ninsufficient data: {len(matched)} reported node(s), fewer than "
            f"{args.min_sample}. Nothing below can be read as evidence either way."
        )
    compare_distributions(matched, nodes)
    compare_against_volume_baseline(matched, nodes)
    print_within_barangay(within_barangay(matched, nodes, args.min_sample), args.min_sample)
    return 0


if __name__ == "__main__":
    sys.exit(main())
