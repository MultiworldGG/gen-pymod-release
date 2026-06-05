"""Bulk per-world wheel driver. Builds a wheel per world from MultiworldGG source,
uploads them to a single GitHub release, and (optionally) rewrites the matching
worlds/<apworld>.json in MultiworldGG-Index to pin the wheel + sha256.

Originally a local one-off; now also the engine behind the build-changed-worlds.yml
reusable workflow in gen-pymod-release. Every repo path and the release destination
is overridable via a SEED_* environment variable (see the constants below), so the
same script runs on a dev box (Windows defaults) and on a CI runner.

Run phases independently so a re-run picks up where the last left off:
  python seed_wheels.py build                  # Phase A — build all Index worlds + manifest
  python seed_wheels.py build --worlds a b c    #          build exactly these from source (no Index)
  python seed_wheels.py upload                  # Phase B — create release + upload
  python seed_wheels.py rewrite-index           # Phase C — rewrite Index manifests
  python seed_wheels.py prune                   # Tidy — remove stale .whl files from staging

State lives under SEED_STAGING (default <source>/tools/.seed-staging/):
  wheels/                  — final .whl files, sha256-named for traceability
  build_manifest.json      — per-world {wheel_filename, sha256, world_version, source}
  failures/<apworld>.log   — full subprocess output for each build failure
"""

from __future__ import annotations

import argparse
import ast
import functools
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tempfile
from pathlib import Path
from typing import Optional

# Repo locations and release destination. Every value is overridable via a SEED_*
# env var so the same script runs on a dev box (Windows defaults below) and on a
# CI runner. An unset OR empty env var falls back to the default.
REPOS = Path(os.environ.get("SEED_REPOS_ROOT") or r"C:\Users\Lindsay\source\repos")
MAIN_REPO = Path(os.environ.get("SEED_SOURCE_ROOT") or (REPOS / "MultiworldGGMain"))
INDEX_REPO = Path(os.environ.get("SEED_INDEX_REPO") or (REPOS / "MultiworldGG-Index"))
# gen-pymod-release supplies shape_tree.py + templates; default to this script's repo.
GEN_PYMOD = Path(os.environ.get("SEED_GEN_PYMOD") or Path(__file__).resolve().parent.parent)

# Interpreter for the `python -m build` subprocess. shape_tree itself runs in THIS
# interpreter (see the tomli_w guard below); on CI both are the same `python`.
VENV_PYTHON = Path(
    os.environ.get("SEED_BUILD_PYTHON")
    or (MAIN_REPO / "tools" / ".seed-venv" / "Scripts" / "python.exe")
)
STAGING = Path(os.environ.get("SEED_STAGING") or (MAIN_REPO / "tools" / ".seed-staging"))
WHEELS_DIR = STAGING / "wheels"
# Per-world build-time source patches (e.g. dk64 ROM-path redirect). Each lives
# at <SEED_OVERRIDES_DIR>/<apworld>/override.py. Applied transiently before
# shape_tree.shape() and restored afterwards — see build_one. Defaults to the copy
# vendored alongside this script in gen-pymod-release.
SEED_OVERRIDES_DIR = Path(
    os.environ.get("SEED_OVERRIDES_DIR") or (GEN_PYMOD / "seed_overrides")
)
FAILURES_DIR = STAGING / "failures"
MANIFEST_PATH = STAGING / "build_manifest.json"

RELEASE_TAG = os.environ.get("SEED_RELEASE_TAG") or f"worlds-wheels-{time.strftime('%Y-%m-%d')}"
RELEASE_REPO = os.environ.get("SEED_RELEASE_REPO") or "MultiworldGG/MultiworldGG-Beta"
RELEASE_TITLE = os.environ.get("SEED_RELEASE_TITLE") or f"World wheels {time.strftime('%Y-%m-%d')}"
RELEASE_NOTES = os.environ.get("SEED_RELEASE_NOTES") or (
    "Seeding of per-world wheels. Each entry is built from the current "
    "MultiworldGG source at the world_version declared in archipelago.json "
    "(or, where missing, the Index entry). These will be superseded as per-world "
    "repos take over via gen-pymod-release."
)

sys.path.insert(0, str(GEN_PYMOD / "scripts"))
import shape_tree  # type: ignore

# shape_tree.shape() runs in THIS interpreter (only `python -m build` uses
# VENV_PYTHON). Without tomli_w importable here, select_or_render_pyproject
# silently drops the [project.entry-points."mwgg.client"] launch hook (and
# version/author injection) — it only warns to stderr, yet still reports the
# entry points in its summary. The resulting wheels look fine but ship no hook,
# so the launcher falls back to the world's __init__ wrapper (e.g. albw's
# launch_subprocess), which double-spawns and opens a second GUI. Refuse to run
# rather than emit silently-broken wheels; .seed-venv ships tomli_w.
if not shape_tree._HAS_TOMLI_W:
    sys.exit(
        f"tomli_w is not importable in {sys.executable}, so shape_tree would "
        "silently drop the mwgg.client launch hook from every client wheel. "
        f"Re-run with the seed venv: {VENV_PYTHON}"
    )


# Fallback set of project-internal modules used if the MAIN_REPO scan misses
# something (e.g., a name that lives only inside a sub-package). The dynamic
# scan in _project_internal_modules() handles the common case.
FIRST_PARTY_FALLBACK = frozenset({
    "worlds_legacy",
})


@functools.lru_cache(maxsize=1)
def _project_internal_modules() -> frozenset[str]:
    """Top-level module/package names defined directly in MAIN_REPO.

    Anything a world imports that resolves to a file or package at the repo
    root is project-internal, not a PyPI dependency.
    """
    out: set[str] = set(FIRST_PARTY_FALLBACK)
    for p in MAIN_REPO.iterdir():
        if p.is_file() and p.suffix == ".py" and p.name != "__init__.py":
            out.add(p.stem)
        elif p.is_dir() and (p / "__init__.py").is_file():
            out.add(p.name)
    return frozenset(out)

# Import-name → PyPI distribution name for the common mismatches.
IMPORT_NAME_MAP = {
    "yaml": "PyYAML",
    "PIL": "Pillow",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "pkg_resources": "setuptools",
    "OpenSSL": "pyOpenSSL",
    "Crypto": "pycryptodome",
}


def scan_imports(world_dir: Path) -> set[str]:
    """Return the top-level module names imported by any .py file under world_dir.

    Only the first segment is kept (so `import bsdiff4.foo` yields `bsdiff4`).
    Relative imports are ignored. Files that fail to parse are skipped with a
    warning — one un-parseable file shouldn't drop the whole world's deps.
    """
    found: set[str] = set()
    for py in world_dir.rglob("*.py"):
        try:
            tree = ast.parse(py.read_bytes(), filename=str(py))
        except (SyntaxError, ValueError) as e:
            print(f"[warn] scan_imports: skipping {py.relative_to(world_dir)}: {e}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue  # relative import
                if node.module:
                    found.add(node.module.split(".", 1)[0])
    return found


def _canonical(name: str) -> str:
    """PEP 503 canonical distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


_REQ_DELIM_RE = re.compile(r"[\s\[=<>!~;@]")


def _dist_name(spec: str) -> str:
    """Extract the canonical distribution name from a requirements.txt line."""
    m = _REQ_DELIM_RE.search(spec)
    head = spec[: m.start()] if m else spec
    return _canonical(head.strip())


def _world_local_module_names(world_dir: Path) -> set[str]:
    """Top-level names that resolve to a file or directory inside this world.

    Worlds routinely ship helper scripts (under ASM/, build_*.py, etc.) that use
    absolute imports like `from Colors import *` because they're meant to be run
    standalone from inside the world dir. These imports resolve to local files,
    not PyPI packages — filter them out of third-party detection.
    """
    out: set[str] = set()
    for p in world_dir.rglob("*"):
        if p.is_file() and p.suffix == ".py" and p.name != "__init__.py":
            out.add(p.stem)
        elif p.is_dir():
            out.add(p.name)
    return out


def detect_third_party_deps(
    apworld: str, world_dir: Path, ignore: frozenset[str] = frozenset()
) -> list[str]:
    """Scan imports, drop stdlib + first-party + self + world-local + `ignore`.

    `ignore` holds import names a world's override declares as non-PyPI (e.g.
    browser/Pyodide-only modules), so they never reach the wheel's Requires-Dist.
    """
    raw = scan_imports(world_dir)
    local = _world_local_module_names(world_dir)
    first_party = _project_internal_modules()
    out: set[str] = set()
    for name in raw:
        if not name:
            continue
        if name in sys.stdlib_module_names:
            continue
        if name in first_party:
            continue
        if name == apworld:
            continue
        if name in local:
            continue
        if name in ignore:
            continue
        out.add(IMPORT_NAME_MAP.get(name, name))
    return sorted(out)


def merge_requirements(original: Optional[str], detected: list[str]) -> Optional[str]:
    """Return new requirements.txt text, or None if no change is needed.

    Existing lines are preserved verbatim (including pins, comments, blanks).
    Detected deps whose canonical name already appears are skipped.
    """
    lines = original.splitlines() if original else []
    have: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            continue  # pip directives; leave alone
        canon = _dist_name(stripped)
        if canon:
            have.add(canon)

    additions = [d for d in detected if _canonical(d) not in have]
    if not additions:
        return None

    new_lines = list(lines)
    if new_lines and new_lines[-1].strip():
        new_lines.append("")
    if original is None:
        new_lines.append("# auto-detected by seed_wheels.py from source imports")
    else:
        new_lines.append("# appended by seed_wheels.py from source imports")
    new_lines.extend(additions)
    return "\n".join(new_lines) + "\n"


def load_manifest() -> dict:
    if MANIFEST_PATH.is_file():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {}


def save_manifest(manifest: dict) -> None:
    STAGING.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def discover_index_entries() -> dict[str, dict]:
    entries: dict[str, dict] = {}
    for path in sorted((INDEX_REPO / "worlds").glob("*.json")):
        apworld = path.stem
        entries[apworld] = json.loads(path.read_text(encoding="utf-8"))
    return entries


def _load_override(apworld: str):
    """Load tools/seed_overrides/<apworld>/override.py, or None if absent.

    The module must expose `TOUCHES: list[str]` (files it reads/creates,
    relative to the world dir) and `apply(world_dir) -> list[str]`.
    """
    path = SEED_OVERRIDES_DIR / apworld / "override.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"seed_override_{apworld}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_one(apworld: str, index_entry: dict, prior_entry: Optional[dict] = None) -> Optional[dict]:
    """Build a single world's wheel. Return manifest entry or None on failure/skip.

    `prior_entry` is the existing manifest entry for this apworld, if any. Used to
    preserve `uploaded_sha256` across rebuilds so cmd_upload can detect when the
    local wheel's bytes have diverged from what's on the GitHub release.
    """
    prior_entry = prior_entry or {}
    world_dir = MAIN_REPO / "worlds" / apworld
    if not world_dir.is_dir():
        print(f"[skip] {apworld}: no source dir at {world_dir}")
        return {"_skipped": True, "reason": "no source dir"}

    archipelago_json_path = world_dir / "archipelago.json"
    original_bytes: Optional[bytes] = None
    if archipelago_json_path.is_file():
        original_bytes = archipelago_json_path.read_bytes()
        existing = json.loads(original_bytes.decode("utf-8"))
    else:
        existing = {}

    # Merge: prefer existing archipelago.json fields; fall back to Index for any missing.
    merged = {
        "game": existing.get("game") or index_entry.get("game", apworld),
        "world_version": existing.get("world_version") or index_entry.get("world_version"),
        "authors": existing.get("authors") or index_entry.get("authors", []),
    }
    for k, v in existing.items():
        if k not in merged:
            merged[k] = v
    if not merged["world_version"]:
        print(f"[skip] {apworld}: no world_version in either archipelago.json or Index entry")
        return {"_skipped": True, "reason": "no world_version available"}

    needs_write = (
        original_bytes is None
        or merged != existing
    )
    if needs_write:
        archipelago_json_path.write_text(
            json.dumps(merged, indent=2) + "\n", encoding="utf-8"
        )

    if original_bytes is None:
        source = "index"
    elif merged != existing:
        source = "archipelago.json+index"
    else:
        source = "archipelago.json"

    # Per-world source patches (e.g. dk64 ROM-path redirect). Loaded early so its
    # IGNORE_IMPORTS feeds the dependency scan; applied (and restored) below.
    override = _load_override(apworld)
    ignore_imports = frozenset(getattr(override, "IGNORE_IMPORTS", []) if override else [])

    # Transient requirements.txt: scan world sources for third-party imports and
    # merge them in so shape_tree's existing parse_requirements_txt() picks them
    # up. Restored (or deleted) in the finally below so the source tree stays clean.
    requirements_path = world_dir / "requirements.txt"
    original_requirements: Optional[bytes] = (
        requirements_path.read_bytes() if requirements_path.is_file() else None
    )
    detected_deps = detect_third_party_deps(apworld, world_dir, ignore=ignore_imports)
    original_text = (
        original_requirements.decode("utf-8") if original_requirements is not None else None
    )
    merged_requirements = merge_requirements(original_text, detected_deps)
    if merged_requirements is not None:
        requirements_path.write_text(merged_requirements, encoding="utf-8")
        added = [
            d for d in detected_deps
            if _canonical(d) not in {
                _dist_name(l) for l in (original_text or "").splitlines() if l.strip()
            }
        ]
        if added:
            print(f"[deps] {apworld}: added {', '.join(added)} from import scan")

    # `override` was loaded above (for IGNORE_IMPORTS). Apply it transiently here
    # so the auto-vendored source tree is restored in the finally below.
    override_backup: dict[str, Optional[bytes]] = {}

    try:
        if override is not None:
            for rel in override.TOUCHES:
                p = world_dir / rel
                override_backup[rel] = p.read_bytes() if p.is_file() else None
            try:
                for change in override.apply(world_dir):
                    print(f"[override] {apworld}: {change}")
            except Exception as e:
                FAILURES_DIR.mkdir(parents=True, exist_ok=True)
                (FAILURES_DIR / f"{apworld}.log").write_text(
                    f"override failed: {e}\n", encoding="utf-8"
                )
                print(f"[fail] {apworld}: override: {e}")
                return None

        with tempfile.TemporaryDirectory(prefix=f"seed-{apworld}-") as tmp:
            output_dir = Path(tmp) / "orphan"
            try:
                shape_tree.shape(
                    source_root=MAIN_REPO,
                    apworld=apworld,
                    templates_dir=GEN_PYMOD / "templates",
                    output_dir=output_dir,
                    caller_repo=RELEASE_REPO,
                    source_ref="seed",
                )
            except SystemExit as e:
                FAILURES_DIR.mkdir(parents=True, exist_ok=True)
                (FAILURES_DIR / f"{apworld}.log").write_text(
                    f"shape_tree failed: {e}\n", encoding="utf-8"
                )
                print(f"[fail] {apworld}: shape_tree: {e}")
                return None

            shape_info = json.loads(
                (output_dir / ".shape_info.json").read_text(encoding="utf-8")
            )
            world_version = shape_info["world_version"]

            result = subprocess.run(
                [str(VENV_PYTHON), "-m", "build", "--wheel", "--no-isolation"],
                cwd=output_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                FAILURES_DIR.mkdir(parents=True, exist_ok=True)
                (FAILURES_DIR / f"{apworld}.log").write_text(
                    f"--- stdout ---\n{result.stdout}\n\n--- stderr ---\n{result.stderr}\n",
                    encoding="utf-8",
                )
                print(f"[fail] {apworld}: python -m build returned {result.returncode}")
                return None

            wheels = list((output_dir / "dist").glob("*.whl"))
            if len(wheels) != 1:
                FAILURES_DIR.mkdir(parents=True, exist_ok=True)
                (FAILURES_DIR / f"{apworld}.log").write_text(
                    f"expected one wheel, got {len(wheels)}: {wheels}\n", encoding="utf-8"
                )
                print(f"[fail] {apworld}: expected one wheel, got {len(wheels)}")
                return None

            wheel = wheels[0]
            wheel_bytes = wheel.read_bytes()
            sha256 = hashlib.sha256(wheel_bytes).hexdigest()
            WHEELS_DIR.mkdir(parents=True, exist_ok=True)
            dest = WHEELS_DIR / wheel.name
            dest.write_bytes(wheel_bytes)
            print(f"[ok]   {apworld}: {wheel.name}  sha256={sha256[:12]}…  ({source})")
            # Preserve `uploaded_sha256` across rebuilds so cmd_upload can detect
            # a mismatch (rebuilt wheel needs to replace the prior upload).
            return {
                "wheel_filename": wheel.name,
                "sha256": sha256,
                "world_version": world_version,
                "source": source,
                "dependencies": list(shape_info.get("dependencies", [])),
                "uploaded_sha256": prior_entry.get("uploaded_sha256"),
            }
    finally:
        if original_bytes is None:
            if archipelago_json_path.is_file():
                archipelago_json_path.unlink()
        elif needs_write:
            archipelago_json_path.write_bytes(original_bytes)
        if merged_requirements is not None:
            if original_requirements is None:
                if requirements_path.is_file():
                    requirements_path.unlink()
            else:
                requirements_path.write_bytes(original_requirements)
        for rel, original in override_backup.items():
            p = world_dir / rel
            if original is None:
                if p.is_file():
                    p.unlink()
            else:
                p.write_bytes(original)


def cmd_build(args: argparse.Namespace) -> int:
    STAGING.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    explicit = list(dict.fromkeys(args.worlds or []))  # de-dupe, preserve order
    if explicit:
        # Source-driven: build exactly these worlds straight from worlds/<name>/,
        # with no Index lookup. Metadata comes from each world's archipelago.json
        # (build_one falls back to {} and skips any world lacking a world_version).
        entries = {name: {} for name in explicit}
    else:
        entries = discover_index_entries()
        only = set(args.only or [])
        if only:
            entries = {k: v for k, v in entries.items() if k in only}

    total = len(entries)
    built = skipped = failed = 0
    print(f"Building wheels for {total} {'requested' if explicit else 'Index'} entries...")

    for i, (apworld, entry) in enumerate(entries.items(), 1):
        prior = manifest.get(apworld, {})
        # Skip only when the prior entry was produced by the current shape_tree
        # contract (i.e., has the `dependencies` field). Legacy entries built
        # before shape_tree learned to read requirements.txt or auto-inject
        # setuptools are unconditionally rebuilt so the user can blanket-fix
        # broken wheels by re-running `build` (no flags). `--force` still
        # overrides every cache check.
        if (
            prior
            and not prior.get("_skipped")
            and not args.force
            and "dependencies" in prior
        ):
            wheel_path = WHEELS_DIR / prior.get("wheel_filename", "")
            if wheel_path.is_file():
                print(f"[have] {apworld}: already built ({i}/{total})")
                built += 1
                continue
        print(f"--- {apworld} ({i}/{total}) ---")
        result = build_one(apworld, entry, prior_entry=prior)
        if result is None:
            failed += 1
        elif result.get("_skipped"):
            skipped += 1
            manifest[apworld] = result
        else:
            built += 1
            manifest[apworld] = result
        if i % 10 == 0:
            save_manifest(manifest)

    save_manifest(manifest)
    print(f"\nDone. built={built} skipped={skipped} failed={failed} total={total}")
    if failed:
        print(f"Failure logs at {FAILURES_DIR}/<apworld>.log")
        return 1
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    manifest = load_manifest()
    only = set(args.only or [])
    # (apworld, wheel_path, entry) for every world the user asked us to consider.
    targets: list[tuple[str, Path, dict]] = []
    for apworld, entry in sorted(manifest.items()):
        if entry.get("_skipped"):
            continue
        if only and apworld not in only:
            continue
        wheel_path = WHEELS_DIR / entry["wheel_filename"]
        if not wheel_path.is_file():
            print(f"[err] missing wheel for {apworld}: {wheel_path}")
            return 1
        targets.append((apworld, wheel_path, entry))

    print(f"Considering {len(targets)} wheels for {RELEASE_REPO}@{RELEASE_TAG}.")

    check = subprocess.run(
        ["gh", "release", "view", RELEASE_TAG, "--repo", RELEASE_REPO],
        capture_output=True, text=True,
    )
    if check.returncode != 0:
        if args.dry_run:
            print(f"(dry-run) Release {RELEASE_TAG} does not yet exist; would create it.")
            existing: set[str] = set()
        else:
            print(f"Creating release {RELEASE_TAG} on {RELEASE_REPO}...")
            create = subprocess.run(
                [
                    "gh", "release", "create", RELEASE_TAG,
                    "--repo", RELEASE_REPO,
                    "--title", RELEASE_TITLE,
                    "--notes", RELEASE_NOTES,
                ],
                text=True,
            )
            if create.returncode != 0:
                print("[err] gh release create failed")
                return 1
            existing = set()
    else:
        view = subprocess.run(
            ["gh", "release", "view", RELEASE_TAG, "--repo", RELEASE_REPO, "--json", "assets"],
            capture_output=True, text=True,
        )
        existing = (
            {a["name"] for a in json.loads(view.stdout).get("assets", [])}
            if view.returncode == 0 else set()
        )

    # Classify each wheel: fresh (asset missing on remote), stale (remote has
    # asset but our manifest's `sha256` doesn't match the previously-recorded
    # `uploaded_sha256` — typically because the wheel was just rebuilt), or
    # in-sync (skip).
    fresh: list[tuple[str, Path]] = []
    stale: list[tuple[str, Path]] = []
    in_sync = 0
    for apworld, wheel_path, entry in targets:
        local_sha = entry["sha256"]
        uploaded_sha = entry.get("uploaded_sha256")
        if wheel_path.name not in existing:
            fresh.append((apworld, wheel_path))
        elif uploaded_sha != local_sha:
            kind = "untracked" if uploaded_sha is None else (
                f"prior_sha={uploaded_sha[:12]}…"
            )
            print(f"[stale] {apworld}: local sha {local_sha[:12]}… vs {kind}; will replace")
            stale.append((apworld, wheel_path))
        else:
            in_sync += 1

    print(f"In-sync: {in_sync}; fresh: {len(fresh)}; stale (will delete+replace): {len(stale)}.")
    if args.dry_run:
        for kind, items in (("fresh", fresh), ("stale", stale)):
            for apworld, wheel_path in items:
                print(f"  {kind:6s} {apworld}  {wheel_path.name}")
        return 0

    # Delete stale assets first so the subsequent re-upload doesn't conflict.
    for apworld, wheel_path in stale:
        print(f"Deleting stale asset {wheel_path.name}...")
        d = subprocess.run(
            ["gh", "release", "delete-asset", RELEASE_TAG, wheel_path.name,
             "--repo", RELEASE_REPO, "--yes"],
            text=True,
        )
        if d.returncode != 0:
            print(f"[err] gh release delete-asset failed for {wheel_path.name}")
            return 1
        existing.discard(wheel_path.name)

    pending = fresh + stale
    chunk_size = 30
    # Track which paths belong to which apworld so we can record uploaded_sha256
    # after each successful chunk.
    pending_by_path = {wheel_path: apworld for apworld, wheel_path in pending}
    for i in range(0, len(pending), chunk_size):
        chunk = pending[i:i + chunk_size]
        print(f"Uploading chunk {i // chunk_size + 1} ({len(chunk)} wheels)...")
        cmd = ["gh", "release", "upload", RELEASE_TAG] + [str(w) for _, w in chunk] + [
            "--repo", RELEASE_REPO,
        ]
        result = subprocess.run(cmd, text=True)
        if result.returncode != 0:
            print(f"[err] gh release upload failed on chunk {i // chunk_size + 1}")
            save_manifest(manifest)  # preserve any successes recorded earlier this run
            return 1
        # Record uploaded_sha256 for each wheel in the chunk so future runs can
        # see they're in-sync.
        for apworld, wheel_path in chunk:
            manifest[apworld]["uploaded_sha256"] = manifest[apworld]["sha256"]
        save_manifest(manifest)

    print("All uploads complete. Verifying...")
    verify = subprocess.run(
        ["gh", "release", "view", RELEASE_TAG, "--repo", RELEASE_REPO, "--json", "assets"],
        capture_output=True, text=True,
    )
    uploaded = {a["name"] for a in json.loads(verify.stdout).get("assets", [])}
    missing = [w.name for _, w, _ in targets if w.name not in uploaded]
    if missing:
        print(f"[err] {len(missing)} wheels missing from release: {missing[:5]}…")
        return 1
    print(f"Verified: all {len(targets)} targeted wheels are on the release.")
    return 0


def cmd_rewrite_index(args: argparse.Namespace) -> int:
    manifest = load_manifest()
    only = set(args.only or [])
    updated = 0
    skipped = 0
    for apworld, entry in sorted(manifest.items()):
        if entry.get("_skipped"):
            skipped += 1
            continue
        if only and apworld not in only:
            continue
        index_file = INDEX_REPO / "worlds" / f"{apworld}.json"
        if not index_file.is_file():
            print(f"[warn] no Index file for {apworld}; skipping")
            skipped += 1
            continue
        data = json.loads(index_file.read_text(encoding="utf-8"))
        wheel_url = (
            f"https://github.com/{RELEASE_REPO}/releases/download/{RELEASE_TAG}/"
            f"{entry['wheel_filename']}#sha256={entry['sha256']}"
        )
        data["module_location"] = wheel_url
        data["world_version"] = entry["world_version"]
        index_file.write_text(
            json.dumps(data, indent=4, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        updated += 1

    print(f"Rewrote {updated} Index manifests; skipped {skipped}.")
    if args.dry_run:
        return 0

    print("Validating against schema...")
    try:
        import jsonschema  # type: ignore
    except ImportError:
        subprocess.run(
            [str(VENV_PYTHON), "-m", "pip", "install", "jsonschema"],
            check=True, capture_output=True,
        )
        import jsonschema  # type: ignore

    schema = json.loads(
        (INDEX_REPO / "schema" / "world_manifest.schema.json").read_text(encoding="utf-8")
    )
    schema_failures = []
    for apworld in manifest:
        if manifest[apworld].get("_skipped"):
            continue
        if only and apworld not in only:
            continue
        index_file = INDEX_REPO / "worlds" / f"{apworld}.json"
        if not index_file.is_file():
            continue
        try:
            jsonschema.validate(
                instance=json.loads(index_file.read_text(encoding="utf-8")),
                schema=schema,
            )
        except jsonschema.ValidationError as e:
            schema_failures.append((apworld, str(e)))
    if schema_failures:
        print(f"[err] {len(schema_failures)} manifests failed schema validation:")
        for apworld, err in schema_failures[:5]:
            print(f"  {apworld}: {err}")
        return 1
    print("All rewritten manifests pass schema validation.")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete stale .whl files from WHEELS_DIR.

    Stale = filename not currently pointed to by any non-skipped manifest entry.
    With --only, the sweep is restricted to wheels belonging to the named worlds
    (so other worlds' stale versions stay until a non-scoped prune).
    """
    manifest = load_manifest()
    if not WHEELS_DIR.is_dir():
        print(f"No wheels dir at {WHEELS_DIR}; nothing to do.")
        return 0

    only = set(args.only or [])
    current_by_world: dict[str, str] = {
        apworld: entry["wheel_filename"]
        for apworld, entry in manifest.items()
        if not entry.get("_skipped") and entry.get("wheel_filename")
    }
    keep = set(current_by_world.values())

    if only:
        # Only sweep wheels belonging to a targeted world. A wheel "belongs to"
        # apworld <X> if its name starts with `worlds_<X>-`.
        prefixes = tuple(f"worlds_{w}-" for w in only)
        candidates = [w for w in WHEELS_DIR.glob("*.whl") if w.name.startswith(prefixes)]
    else:
        candidates = list(WHEELS_DIR.glob("*.whl"))

    removed = kept = 0
    for whl in sorted(candidates):
        if whl.name in keep:
            kept += 1
            continue
        if args.dry_run:
            print(f"[would-remove] {whl.name}")
        else:
            whl.unlink()
            print(f"[clean] removed stale wheel {whl.name}")
        removed += 1

    verb = "would remove" if args.dry_run else "removed"
    print(f"Done. {verb}={removed} kept={kept}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="Phase A — build wheels locally")
    p_build.add_argument("--only", nargs="*", help="Only build these Index apworlds")
    p_build.add_argument(
        "--worlds", nargs="*",
        help="Build exactly these apworlds straight from source, bypassing the Index "
             "(takes precedence over --only)",
    )
    p_build.add_argument("--force", action="store_true", help="Rebuild even if manifest entry exists")
    p_build.set_defaults(func=cmd_build)

    p_upload = sub.add_parser(
        "upload",
        help="Phase B — create release + upload wheels (auto-replaces wheels "
             "whose local sha256 differs from the recorded uploaded_sha256)",
    )
    p_upload.add_argument("--only", nargs="*", help="Only consider these apworlds")
    p_upload.add_argument("--dry-run", action="store_true", help="Print what would happen, don't upload")
    p_upload.set_defaults(func=cmd_upload)

    p_rewrite = sub.add_parser("rewrite-index", help="Phase C — rewrite Index manifests")
    p_rewrite.add_argument("--only", nargs="*", help="Only rewrite these apworlds")
    p_rewrite.add_argument("--dry-run", action="store_true", help="Rewrite files but don't validate")
    p_rewrite.set_defaults(func=cmd_rewrite_index)

    p_prune = sub.add_parser(
        "prune",
        help="Remove stale .whl files from .seed-staging/wheels/ (anything not "
             "pointed to by the current manifest)",
    )
    p_prune.add_argument("--only", nargs="*", help="Restrict sweep to these apworlds' wheels")
    p_prune.add_argument("--dry-run", action="store_true", help="List what would be deleted")
    p_prune.set_defaults(func=cmd_prune)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
