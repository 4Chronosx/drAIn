"""Filesystem locations for the bundled SWMM model and its artifacts."""

from __future__ import annotations

from pathlib import Path

#: Repository root, resolved relative to this file so the server behaves the
#: same regardless of the working directory it is launched from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = PROJECT_ROOT / "data"

#: The unmodified Mandaue drainage network and its pre-computed results.
BASE_INP = DATA_DIR / "Mandaue_Drainage_Network.inp"
BASE_RPT = DATA_DIR / "Mandaue_Drainage_Network.rpt"
BASE_OUT = DATA_DIR / "Mandaue_Drainage_Network.out"

#: Trained k-means vulnerability model.
VULNERABILITY_MODEL = DATA_DIR / "vulnerability_model_k4.pkl"


def mod_artifacts(inp_path: Path) -> tuple[Path, Path]:
    """Return the ``.rpt`` and ``.out`` paths pyswmm writes for a pre-configured run.

    ``SimulationPreConfig`` materialises a modified input file alongside the
    original with a ``_mod`` suffix, and SWMM then writes its report and binary
    output next to that file.
    """
    stem = f"{inp_path.stem}_mod"
    return inp_path.with_name(f"{stem}.rpt"), inp_path.with_name(f"{stem}.out")
