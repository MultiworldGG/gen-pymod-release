"""Seed-build override for Paper Mario: dedupe auto-hint scouts by location id.

``client.py`` ``game_watcher`` scouts the shop locations in the player's current
area (``location_groups["AutoHint"]``) and remembers what it has already scouted
in ``self.autohint_stored`` / ``self.autohint_released``. Both hold location
*ids* (``hints`` is mapped through ``location_name_to_id`` before it is stored),
but the dedupe filter compares the group's location *names* against them, so it
never matches. Every watcher tick (``ctx.watcher_timeout = 1``) spent in an area
with AutoHint locations therefore re-sends a ``LocationScouts`` with
``create_as_hint: 0`` and another with ``create_as_hint: 2``; each reply is a
``LocationInfo`` packet that makes the Universal Tracker overlay re-run its
logic and refresh the hint screen about twice a second. This override rewrites
the filter to look the name's id up first, so scouted locations are skipped and
each area is scouted once.

Applied transiently at wheel-build time by scripts/seed_wheels.py (see
``_load_override`` / ``build_one``); the source tree is restored afterwards.

Contract used by seed_wheels.py:
  TOUCHES        - files this override reads or creates; snapshotted before
                   apply() and restored (or deleted) afterwards.
  apply(world_dir) -> list[str]
                 - performs the edits and returns human-readable change
                   descriptions. Raises if any anchor is missing or ambiguous,
                   so upstream drift can never ship a half-patched wheel.
"""
from __future__ import annotations

from pathlib import Path

TOUCHES = ["client.py"]

# (relative_path, anchor, replacement). Each anchor must appear exactly once.
_EDITS = [
    (
        "client.py",
        '                for loc in location_groups["AutoHint"]:\n'
        "                    if loc not in self.autohint_released.union(self.autohint_stored):\n",
        '                for loc in location_groups["AutoHint"]:\n'
        "                    if location_name_to_id[loc] not in self.autohint_released.union(self.autohint_stored):\n",
    ),
]


def _replace_once(text: str, old: str, new: str, *, where: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ValueError(
            f"papermario override: expected exactly 1 occurrence of anchor in {where}, "
            f"found {count}. Upstream source has drifted; update seed_overrides/papermario."
        )
    return text.replace(old, new)


def apply(world_dir: Path) -> list[str]:
    changes: list[str] = []
    by_file: dict[str, list[tuple[str, str]]] = {}
    for rel, old, new in _EDITS:
        by_file.setdefault(rel, []).append((old, new))

    for rel, edits in by_file.items():
        path = world_dir / rel
        if not path.is_file():
            raise FileNotFoundError(f"papermario override: target missing: {rel}")
        text = path.read_text(encoding="utf-8")
        for old, new in edits:
            text = _replace_once(text, old, new, where=rel)
        # newline="" so we don't translate the source's LF endings to CRLF on
        # Windows - a flip would dirty the (LF) tree on restore.
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        changes.append(f"patched {rel} ({len(edits)} edit(s))")
    return changes
