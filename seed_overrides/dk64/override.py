"""Seed-build override for DK64: resolve the base ROM from host settings.

DK64's vendored randomizer reads the vanilla ``dk64.z64`` via a bare, cwd-relative
path in four places (the generate-stage gate, ``Patcher.load_base_rom``,
``LocalROM`` and the ``ApplyRandomizer`` xdelta step) and otherwise pops a Tk file
picker. Neither works on a headless webhost. This override redirects every read
through a new ``_ap_rom.resolve_rom_path()`` that resolves an absolute path from
``host.yaml`` (``dk64_options.rom_file``), so the ROM can live anywhere — no cwd
dependence, no GUI prompt.

``worlds/dk64`` is auto-vendored from 2dos/DK64-Randomizer-Dev, so this is applied
transiently at wheel-build time by tools/seed_wheels.py (see ``_load_override`` /
``build_one``) and the source tree is restored afterwards.

Contract used by seed_wheels.py:
  TOUCHES        — files this override reads or creates; snapshotted before
                   apply() and restored (or deleted) afterwards.
  apply(world_dir) -> list[str]
                 — performs the edits, creates _ap_rom.py, returns human-readable
                   change descriptions. Raises if any anchor is missing or
                   ambiguous, so upstream drift can never ship a half-patched wheel.
"""
from __future__ import annotations

from pathlib import Path

TOUCHES = [
    "__init__.py",
    "randomizer/Patching/Patcher.py",
    "randomizer/Patching/ApplyRandomizer.py",
    "_ap_rom.py",
]

# Browser/Pyodide-only imports the randomizer makes that are not PyPI packages
# (e.g. `from ui.progress_bar import ProgressBar` in ApplyLocal.py). Excluded
# from the dependency scan so they don't land in the wheel's Requires-Dist.
IGNORE_IMPORTS = ["ui"]

# Written verbatim to worlds/dk64/_ap_rom.py. A str subclass (settings.UserFilePath)
# is returned by `.rom_file`; its `.resolve()` yields an absolute path, or
# user_path(<relative>) for a relative host.yaml value.
_AP_ROM_PY = '''\
"""Resolve the DK64 base ROM path from host settings.

Injected at wheel-build time by tools/seed_overrides/dk64. Replaces the
randomizer's original cwd-relative ``dk64.z64`` lookups, which cannot work on a
headless webhost (no stable working directory, no GUI file picker).

Resolution order:
  1. host.yaml ``dk64_options.rom_file`` (a settings.UserFilePath whose
     ``.resolve()`` returns an absolute path, or ``user_path(<relative>)``)
  2. ``Utils.user_path("dk64.z64")``
  3. cwd-relative ``dk64.z64`` (original behaviour, last resort)
"""
from __future__ import annotations

import os


def resolve_rom_path() -> str:
    """Absolute (or last-resort relative) path to the vanilla DK64 ROM."""
    try:
        from settings import get_settings

        rom = get_settings()["dk64_options"].rom_file
        resolved = rom.resolve() if hasattr(rom, "resolve") else str(rom)
        if resolved:
            return str(resolved)
    except Exception:
        pass
    try:
        from Utils import user_path

        return user_path("dk64.z64")
    except Exception:
        return "dk64.z64"
'''

_IMPORT = "from worlds.dk64._ap_rom import resolve_rom_path"

# (relative_path, anchor, replacement). Each anchor must appear exactly once.
_EDITS = [
    # 1. Expose the ROM as a configurable host setting (UserFilePath -> abs path).
    (
        "__init__.py",
        '        release_branch: ReleaseVersion = ReleaseVersion("master")\n'
        "        enable_minimal_logic_dk64: EnableMinimalLogic | bool = False\n",
        "        class RomFile(settings.UserFilePath):\n"
        '            """Vanilla, big-endian Donkey Kong 64 .z64 ROM (CRC32 D44B4FC6)."""\n'
        "\n"
        '            description = "Donkey Kong 64 ROM"\n'
        "            required = False\n"
        "\n"
        '        release_branch: ReleaseVersion = ReleaseVersion("master")\n'
        "        enable_minimal_logic_dk64: EnableMinimalLogic | bool = False\n"
        '        rom_file: RomFile = RomFile("dk64.z64")\n',
    ),
    # 2. generate-stage gate: resolve from settings instead of cwd "dk64.z64".
    (
        "__init__.py",
        '            rom_file = "dk64.z64"\n',
        f"            {_IMPORT}\n"
        "            rom_file = resolve_rom_path()\n",
    ),
    # 3. The picker fallback copies to rom_file, now an absolute path — ensure its
    #    parent exists.
    (
        "__init__.py",
        "                try:\n"
        "                    shutil.copy(file, rom_file)\n",
        "                try:\n"
        '                    os.makedirs(os.path.dirname(rom_file) or ".", exist_ok=True)\n'
        "                    shutil.copy(file, rom_file)\n",
    ),
    # 4. Patcher.load_base_rom: read the resolved ROM.
    (
        "randomizer/Patching/Patcher.py",
        '        original = open("dk64.z64", "rb")\n',
        f"        {_IMPORT}\n"
        "        original = open(resolve_rom_path(), \"rb\")\n",
    ),
    # 5. LocalROM existence guard + a headless-friendly error message.
    (
        "randomizer/Patching/Patcher.py",
        '            if not os.path.exists("dk64.z64"):\n'
        '                raise Exception("No ROM was loaded, please make sure you have dk64.z64 in the root directory of the project.")\n',
        f"            {_IMPORT}\n"
        "            if not os.path.exists(resolve_rom_path()):\n"
        '                raise Exception("No DK64 ROM found. Set dk64_options.rom_file in host.yaml to a vanilla big-endian DK64 .z64 (CRC32 D44B4FC6).")\n',
    ),
    # 6. ApplyRandomizer xdelta: diff against the resolved vanilla ROM.
    (
        "randomizer/Patching/ApplyRandomizer.py",
        '        pyxdelta.run("dk64.z64", created_tempfile, delta_tempfile)\n',
        f"        {_IMPORT}\n"
        "        pyxdelta.run(resolve_rom_path(), created_tempfile, delta_tempfile)\n",
    ),
]


def _replace_once(text: str, old: str, new: str, *, where: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ValueError(
            f"dk64 override: expected exactly 1 occurrence of anchor in {where}, "
            f"found {count}. Upstream source has drifted; update tools/seed_overrides/dk64."
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
            raise FileNotFoundError(f"dk64 override: target missing: {rel}")
        text = path.read_text(encoding="utf-8")
        for old, new in edits:
            text = _replace_once(text, old, new, where=rel)
        # newline="" so we don't translate the source's LF endings to CRLF on
        # Windows — a flip would dirty the (LF) vendored tree on restore.
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        changes.append(f"patched {rel} ({len(edits)} edit(s))")

    with open(world_dir / "_ap_rom.py", "w", encoding="utf-8", newline="") as fh:
        fh.write(_AP_ROM_PY)
    changes.append("created _ap_rom.py")
    return changes
