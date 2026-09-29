"""How much does the hazard ranking depend on its own settings?

The hazard score (drain/hazard.py) weighs flood volume 0.5, time flooded
0.3 and peak rate 0.2, each against a full-scale reference taken from the
baseline run's 95th percentile. None of those five numbers comes from a
damage study. If the top of the work list reshuffles whenever they move a
little, the list says more about the settings than about the drains.

This perturbs them and measures how the ranking of the baseline run's
flooded nodes holds up (roadmap A1):

- weights drawn from a Dirichlet centred on (0.5, 0.3, 0.2);
- each full-scale reference multiplied by a log-uniform factor in [0.5, 2];
- per draw: Kendall's tau against the baseline ranking, the Jaccard overlap
  of the top 50, and for each baseline top-50 node whether it stays there.

Usage::

    python -m scripts.sensitivity --samples 2000 --out docs/sensitivity-2026-09-29.md

Reads only the shipped baseline report; runs no simulation.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
from scipy.stats import kendalltau

from drain import hazard
from drain.hazard import DEFAULT_EVENT_HOURS
from drain.paths import BASE_RPT
from drain.rpt_parser import parse_flooding_summary

#: How tightly the Dirichlet draws cluster around the base weights. At 20 a
#: typical draw moves each weight by about 0.1: (0.5, 0.3, 0.2) often
#: becomes something like (0.42, 0.36, 0.22).
CONCENTRATION = 20.0

#: Full-scale references are multiplied by a factor between these.
SCALE_RANGE = (0.5, 2.0)

TOP_N = 50


@dataclass(frozen=True)
class Flooding:
    """The baseline's flooded nodes and their three measurements."""

    node_ids: list[str]
    volume: np.ndarray
    hours: np.ndarray
    rate: np.ndarray


def load_baseline(rpt_path: Path = BASE_RPT) -> Flooding:
    flooded = {
        node_id: summary
        for node_id, summary in parse_flooding_summary(rpt_path).items()
        if summary.hours_flooded > 0
        or summary.total_flood_volume > 0
        or summary.maximum_rate_cms > 0
    }
    ids = sorted(flooded)
    return Flooding(
        node_ids=ids,
        volume=np.array([flooded[n].total_flood_volume for n in ids]),
        hours=np.array([flooded[n].hours_flooded for n in ids]),
        rate=np.array([flooded[n].maximum_rate_cms for n in ids]),
    )


def scores(
    data: Flooding,
    weights: tuple[float, float, float],
    volume_full_scale: float,
    rate_full_scale: float,
    event_hours: float = DEFAULT_EVENT_HOURS,
) -> np.ndarray:
    """The hazard score of every node, as drain.hazard computes it."""
    volume_weight, duration_weight, rate_weight = weights
    return (
        volume_weight * np.clip(data.volume / volume_full_scale, 0, 1)
        + duration_weight * np.clip(data.hours / event_hours, 0, 1)
        + rate_weight * np.clip(data.rate / rate_full_scale, 0, 1)
    )


def top(values: np.ndarray, n: int) -> set[int]:
    """Indices of the n highest values; ties broken by position (node id)."""
    order = np.lexsort((np.arange(len(values)), -values))
    return set(order[:n].tolist())


@dataclass(frozen=True)
class Result:
    samples: int
    nodes: int
    tau: np.ndarray
    jaccard: np.ndarray
    #: Baseline top-N node id -> share of draws it stayed in the top N.
    survival: dict[str, float]


def run(
    data: Flooding,
    samples: int,
    seed: int = 7,
    concentration: float = CONCENTRATION,
    scale_range: tuple[float, float] = SCALE_RANGE,
    top_n: int = TOP_N,
) -> Result:
    rng = np.random.default_rng(seed)
    base_weights = (hazard.VOLUME_WEIGHT, hazard.DURATION_WEIGHT, hazard.RATE_WEIGHT)
    base = scores(data, base_weights, hazard.VOLUME_FULL_SCALE, hazard.RATE_FULL_SCALE)
    base_top = top(base, top_n)

    low, high = np.log(scale_range[0]), np.log(scale_range[1])
    taus = np.empty(samples)
    jaccards = np.empty(samples)
    kept = dict.fromkeys(base_top, 0)

    for i in range(samples):
        weights = tuple(rng.dirichlet(concentration * np.array(base_weights)))
        volume_scale = hazard.VOLUME_FULL_SCALE * np.exp(rng.uniform(low, high))
        rate_scale = hazard.RATE_FULL_SCALE * np.exp(rng.uniform(low, high))
        drawn = scores(data, weights, volume_scale, rate_scale)

        taus[i] = kendalltau(base, drawn).statistic
        drawn_top = top(drawn, top_n)
        jaccards[i] = len(base_top & drawn_top) / len(base_top | drawn_top)
        for index in base_top & drawn_top:
            kept[index] += 1

    return Result(
        samples=samples,
        nodes=len(data.node_ids),
        tau=taus,
        jaccard=jaccards,
        survival={data.node_ids[i]: count / samples for i, count in kept.items()},
    )


def _band(values: np.ndarray) -> str:
    p5, p50, p95 = np.percentile(values, [5, 50, 95])
    return f"{p50:.3f} ({p5:.3f} to {p95:.3f})"


def report(result: Result, top_n: int = TOP_N, on: date | None = None) -> str:
    survival = result.survival
    solid = sum(1 for share in survival.values() if share >= 0.9)
    shaky = sorted((share, node) for node, share in survival.items() if share < 0.5)
    less_sure = sorted((share, node) for node, share in survival.items() if share < 0.9)
    lines = [
        f"# Hazard ranking sensitivity ({(on or date.today()).isoformat()})",
        "",
        "Generated by `scripts/sensitivity.py` (roadmap A1). It perturbs the hazard",
        "score's weights and full-scale references and asks how much the ranking of",
        f"the baseline run's {result.nodes} flooded nodes moves.",
        "",
        "## Settings",
        "",
        f"- {result.samples} draws.",
        f"- Weights: Dirichlet centred on (0.5, 0.3, 0.2), concentration {CONCENTRATION:g}.",
        f"- Full-scale references ({hazard.VOLUME_FULL_SCALE:g} ML volume, "
        f"{hazard.RATE_FULL_SCALE:g} m³/s peak rate): each multiplied by a "
        f"log-uniform factor in [{SCALE_RANGE[0]:g}, {SCALE_RANGE[1]:g}].",
        "- Only flooded nodes are ranked; the rest all score 0 in every draw.",
        "",
        "## Results",
        "",
        "| Measure | Median (5th to 95th percentile) |",
        "|---|---|",
        f"| Kendall's τ against the baseline ranking | {_band(result.tau)} |",
        f"| Jaccard overlap of the top {top_n} | {_band(result.jaccard)} |",
        "",
        f"Of the baseline's top {top_n} nodes, **{solid}** stay in the top {top_n} "
        f"in at least 90% of draws, and **{len(shaky)}** in fewer than half.",
        "",
    ]
    if less_sure:
        lines += [
            f"Top-{top_n} nodes that drop out in more than 1 draw in 10:",
            "",
            "| Node | Share of draws still in the top " + str(top_n) + " |",
            "|---|---|",
            *[f"| {node} | {share:.0%} |" for share, node in less_sure],
            "",
        ]
    lines += [
        "## Reading it",
        "",
        "- τ near 1 means the settings barely matter to the overall order; the",
        "  work list is driven by the flooding, not by the weights.",
        f"- The top-{top_n} figures matter more: that is the list an agency acts",
        "  on. Nodes that survive most draws are a safe first list whatever the",
        "  exact weights; the rest belong to a second pass.",
        "- This says nothing about whether the model is *right*, only whether its",
        "  ranking is stable. Validation against reports is roadmap C.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, help="Write the markdown report here.")
    args = parser.parse_args(argv)

    result = run(load_baseline(), samples=args.samples, seed=args.seed)
    text = report(result)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
