"""The pre-commit hook says how to install ruff when it can't find it."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".githooks" / "pre-commit"
SH = shutil.which("sh")


@pytest.mark.skipif(SH is None, reason="sh not found")
def test_hook_explains_missing_ruff(tmp_path):
    hook = tmp_path / "pre-commit"
    shutil.copyfile(HOOK, hook)
    # PATH holds only sh's own directory, and tmp_path has no .venv: no ruff anywhere.
    result = subprocess.run(
        [SH, str(hook)],
        cwd=tmp_path,
        env={"PATH": str(Path(SH).parent)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert (
        "ruff not found: create .venv and run pip install --require-hashes -r requirements-dev.txt"
        in result.stderr
    )
