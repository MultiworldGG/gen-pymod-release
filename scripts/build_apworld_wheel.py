"""Build a world's pip wheel locally, the same artifact CI's build.yml produces.

For authors with a full Archipelago fork who do NOT use the reusable CI
workflow and want to build the wheel by hand, then attach it to their GitHub
release manually.

Run from the fork root:

    python tools/build/scripts/build_apworld_wheel.py --apworld <folder>

Reads `game` and `world_version` from `worlds/<apworld>/archipelago.json`,
shapes the orphan wheel-source tree with the sibling `shape_tree.py` (exactly
as build.yml does), runs `python -m build --wheel`, and copies the resulting
`worlds_<apworld>-<world_version>-py3-none-any.whl` into ./dist/.

Prerequisite: `python -m pip install build tomli_w`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
# templates/ sits next to scripts/ in both layouts: this repo (scripts/ +
# templates/) and the release bundle (tools/build/scripts/ + tools/build/templates/).
_TEMPLATES_DIR = _SCRIPTS_DIR.parent / "templates"


def _load_shape_tree():
    path = _SCRIPTS_DIR / "shape_tree.py"
    spec = importlib.util.spec_from_file_location("shape_tree", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["shape_tree"] = module
    spec.loader.exec_module(module)
    return module


def _require_build_module() -> None:
    if importlib.util.find_spec("build") is None:
        raise SystemExit(
            "The 'build' package is required. Install it with:\n"
            "    python -m pip install build tomli_w"
        )


def _caller_repo_from_manifest(manifest_path: Path) -> str:
    """Cosmetic metadata only (lands in .shape_info.json, not in the wheel)."""
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "local/local"
    repo_url = str(data.get("repo_url", "")).strip()
    slug = repo_url.removeprefix("https://github.com/").removesuffix(".git").strip("/")
    return slug if slug.count("/") == 1 else "local/local"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apworld", required=True,
                        help="World folder name under worlds/<apworld>/.")
    parser.add_argument("--source-root", type=Path, default=Path("."),
                        help="Fork root containing worlds/<apworld>/ (default: current dir).")
    parser.add_argument("--output-dir", type=Path, default=Path("dist"),
                        help="Directory to copy the finished wheel into (default: ./dist).")
    parser.add_argument("--source-ref", default="local",
                        help="Cosmetic build-metadata ref recorded in .shape_info.json.")
    parser.add_argument("--keep-tree", action="store_true",
                        help="Keep the intermediate shaped tree for inspection.")
    args = parser.parse_args(argv)

    _require_build_module()
    shape_tree = _load_shape_tree()

    source_root = args.source_root.resolve()
    manifest_path = source_root / "worlds" / args.apworld / "archipelago.json"
    caller_repo = _caller_repo_from_manifest(manifest_path)

    if (source_root / "worlds" / args.apworld / "pyproject.toml").is_file() \
            and importlib.util.find_spec("tomli_w") is None:
        print(
            "::warning::worlds/{0}/pyproject.toml ships its own metadata but tomli_w "
            "is not installed, so version/authors injection is skipped (the wheel may "
            "carry a stale version). Run: python -m pip install tomli_w".format(args.apworld),
            file=sys.stderr,
        )

    build_root = Path(tempfile.mkdtemp(prefix=f"{args.apworld}-wheel-"))
    tree_dir = build_root / "orphan-tree"
    try:
        # Same shaping build.yml runs before `python -m build --wheel`.
        shape_tree.shape(
            source_root=source_root,
            apworld=args.apworld,
            templates_dir=_TEMPLATES_DIR,
            output_dir=tree_dir,
            caller_repo=caller_repo,
            source_ref=args.source_ref,
        )

        subprocess.run([sys.executable, "-m", "build", "--wheel"],
                       cwd=tree_dir, check=True)

        wheels = sorted((tree_dir / "dist").glob("*.whl"))
        if len(wheels) != 1:
            raise SystemExit(
                f"Expected exactly one wheel under {tree_dir / 'dist'}, got {len(wheels)}."
            )

        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        final_wheel = output_dir / wheels[0].name
        shutil.copy2(wheels[0], final_wheel)
    finally:
        if args.keep_tree:
            print(f"Kept shaped tree at {tree_dir}")
        else:
            shutil.rmtree(build_root, ignore_errors=True)

    print(f"\nBuilt wheel: {final_wheel}")
    print("Attach this file to your GitHub release.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
