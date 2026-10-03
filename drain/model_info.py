"""What a result can and cannot claim, sent with every result.

A city engineer reading "No hazard" next to a node should know that it
means "this model, as built, did not flood it" -- not "this drain is safe".
This block travels in the payload's metadata so the app can say that where
the ratings are read, without keeping its own copy of the numbers in step.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from drain import hazard
from drain.paths import BASE_INP

_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


@lru_cache(maxsize=1)
def network_built_on(inp_path: Path = BASE_INP) -> str | None:
    """The date in the network file's [TITLE] section: when it was generated
    from the GIS data. ``None`` if the title carries no date."""
    in_title = False
    with open(inp_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith("["):
                if in_title:
                    return None
                in_title = stripped.upper() == "[TITLE]"
                continue
            if in_title and (match := _DATE.search(stripped)):
                return match.group(1)
    return None


@lru_cache(maxsize=1)
def network_sha256(inp_path: Path = BASE_INP) -> str:
    """A fingerprint of the network file. Two runs with the same one used
    the same model; any edit to the .inp changes it."""
    digest = hashlib.sha256()
    with open(inp_path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def model_info() -> dict[str, Any]:
    """The model's provenance and limits, in a form the app can render."""
    return {
        "network": "Mandaue City drainage network (SWMM)",
        "network_built_on": network_built_on(),
        "network_sha256": network_sha256(),
        # Nothing here has been checked against measured flood depths or
        # field records of where it floods.
        "calibrated": False,
        "hazard_score": {
            "weights": {
                "flood_volume": hazard.VOLUME_WEIGHT,
                "duration_share_of_storm": hazard.DURATION_WEIGHT,
                "peak_rate": hazard.RATE_WEIGHT,
            },
            "full_scale": {
                "flood_volume_megalitres": hazard.VOLUME_FULL_SCALE,
                "peak_rate_cms": hazard.RATE_FULL_SCALE,
            },
            "category_thresholds": {name: floor for floor, name in hazard.CATEGORY_THRESHOLDS},
            # The reference values are the baseline run's 95th percentile,
            # not the result of a damage study.
            "provisional": True,
        },
        "exposure": "Population density of the barangay a node is in, not people in the flood.",
        "not_modelled": [
            "Blocked or silted drains: every pipe is modelled clean.",
            "High tide or storm surge at the outfalls: they discharge freely.",
            "Ground already wet from earlier rain.",
            "Real storm timing: the storm is an idealised triangle.",
            "Changes to the city since the network was built.",
        ],
    }
