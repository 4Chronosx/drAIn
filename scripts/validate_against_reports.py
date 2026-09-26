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

Usage::

    SUPABASE_URL=... SUPABASE_KEY=... python -m scripts.validate_against_reports

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


@dataclass(frozen=True)
class Report:
    component_id: str
    category: str | None
    created_at: str | None


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
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        query = urllib.parse.urlencode(
            {
                "select": "component_id,category,created_at",
                "review_status": "neq.rejected",
                "order": "id",
                "limit": PAGE_SIZE,
                "offset": offset,
            }
        )
        page = get_json(f"{url.rstrip('/')}/rest/v1/reports?{query}", headers)
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return [
        Report(
            component_id=str(row["component_id"]),
            category=row.get("category"),
            created_at=row.get("created_at"),
        )
        for row in rows
        if row.get("component_id")
    ]


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
    args = parser.parse_args(argv)

    configure_logging(args.log_level.upper())

    if not args.supabase_url or not args.supabase_key:
        parser.error("Set SUPABASE_URL and SUPABASE_KEY, or pass --supabase-url/--supabase-key.")

    try:
        reports = fetch_reports(args.supabase_url, args.supabase_key)
    except Exception as error:
        print(f"Could not read the reports table: {error}", file=sys.stderr)
        return 1

    # The baseline network: what the model says with no storm applied.
    nodes = build_flooding_summary(BASE_RPT, BASE_OUT)["nodes_dict"]

    matched = summarise_join(reports, nodes)
    compare_distributions(matched, nodes)
    compare_against_volume_baseline(matched, nodes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
