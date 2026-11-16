"""Seed-build override for DK64: dependency-scan exclusions only.

Upstream generation no longer reads the vanilla ROM (AP returns from
``Generate_Spoiler`` before ``patching_response`` and ships a proto patch), so
no source edits are needed.
"""
from __future__ import annotations

from pathlib import Path

TOUCHES: list[str] = []

# Browser/Pyodide-only imports the randomizer makes that are not PyPI packages
# (e.g. `from ui.progress_bar import ProgressBar` in ApplyLocal.py). Excluded
# from the dependency scan so they don't land in the wheel's Requires-Dist.
IGNORE_IMPORTS = ["ui"]


def apply(world_dir: Path) -> list[str]:
    return []
