"""Command-line entry point for running a simulation without the HTTP server.

Replaces the ad-hoc ``test.py`` scratch script::

    python -m drain.cli --precip 400 --duration 24 --node I-4
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from app.logging_config import configure_logging
from drain.flooding import build_flooding_summary
from drain.hazard import DEFAULT_EVENT_HOURS
from drain.swmm_runner import run_simulation

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--precip", type=float, default=None, help="Total rainfall depth in mm.")
    parser.add_argument("--duration", type=float, default=None, help="Storm duration in hours.")
    parser.add_argument(
        "--node",
        action="append",
        default=[],
        dest="nodes",
        metavar="NODE_ID",
        help="Print this node's results. Repeatable. Omit to print a summary only.",
    )
    parser.add_argument("--json", type=argparse.FileType("w"), help="Write the full payload here.")
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level.upper())

    if (args.precip is None) != (args.duration is None):
        logger.error("--precip and --duration must be given together.")
        return 2

    rainfall = (
        {"total_precip": args.precip, "duration_hr": args.duration}
        if args.precip is not None
        else {}
    )

    event_hours = float(rainfall.get("duration_hr") or DEFAULT_EVENT_HOURS)

    with run_simulation(rainfall=rainfall) as (rpt_path, out_path):
        summary = build_flooding_summary(rpt_path, out_path, event_hours=event_hours)

    metadata = summary["metadata"]
    print(
        f"{metadata['total_nodes']} nodes, "
        f"{metadata['flooded_nodes']} flooded, "
        f"{metadata['non_flooded_nodes']} dry"
    )

    for node_id in args.nodes:
        row = summary["nodes_dict"].get(node_id)
        if row is None:
            logger.warning("No such node: %s", node_id)
        else:
            print(f"{node_id}: {json.dumps(row, indent=2)}")

    if args.json:
        json.dump(summary, args.json, indent=2)
        logger.info("Wrote full payload to %s", args.json.name)

    return 0


if __name__ == "__main__":
    sys.exit(main())
