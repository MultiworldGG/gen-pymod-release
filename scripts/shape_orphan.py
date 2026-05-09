"""Build the wheel-source tree for the build-and-publish-action.

Inputs:
  - source_root     : checkout of the caller repo (must contain worlds/<slug>/)
  - slug            : world slug
  - templates_dir   : path to this action's templates/ directory
  - output_dir      : where to write the orphan-shaped tree

Outputs (under output_dir):
  pyproject.toml
  .shape_info.json   {slug, world_version, game} — single source of truth
                     for downstream workflow steps; avoids re-parsing
                     archipelago.json from build.yml.
  src/
    worlds/
      <slug>/
        ...world source...

If the caller's repo ships `worlds/<slug>/pyproject.toml`, it is used as-is —
with `version` and `authors` injected from `archipelago.json` only if those
fields are absent (mirrors the `tools/build_wheels.py` pattern in the
MultiworldGG monorepo).

Otherwise, the bundled `templates/pyproject.toml.j2` fallback is rendered.

Pure functions; the workflow shells out to this with the right args.
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sys
from pathlib import Path
from typing import Optional

# Toml: read with stdlib (3.11+), write with stdlib if available; otherwise tomli/tomli_w.
import tomllib  # 3.11+
try:
    import tomli_w  # for round-tripping with version/author injection
    _HAS_TOMLI_W = True
except ImportError:
    _HAS_TOMLI_W = False


def read_archipelago_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def render_jinja_template(template_path: Path, **kwargs) -> str:
    """Minimal Jinja-style substitution. We don't pull in Jinja2 to keep the
    runtime deps to stdlib only.

    Supports: {{ var }} and {% for x in list %}...{% endfor %}.
    """
    text = template_path.read_text(encoding="utf-8")
    # Handle {% for %} blocks first.
    import re

    def expand_for(match: re.Match) -> str:
        var, expr, body = match.group(1), match.group(2), match.group(3)
        items = kwargs.get(expr, [])
        out = []
        for item in items:
            local = dict(kwargs)
            local[var] = item
            out.append(_render_simple(body, local))
        return "".join(out)

    text = re.sub(
        r"\{%\s*for\s+(\w+)\s+in\s+(\w+)\s*%\}(.*?)\{%\s*endfor\s*%\}",
        expand_for,
        text,
        flags=re.DOTALL,
    )
    return _render_simple(text, kwargs)


def _render_simple(text: str, kwargs: dict) -> str:
    import re
    def sub(m: "re.Match") -> str:
        key = m.group(1).strip()
        value = kwargs
        for part in key.split("."):
            if isinstance(value, dict):
                value = value.get(part, "")
            else:
                value = getattr(value, part, "")
        return str(value)
    return re.sub(r"\{\{\s*([^}]+?)\s*\}\}", sub, text)


def select_or_render_pyproject(
    *,
    caller_world_dir: Path,
    slug: str,
    archipelago_json: dict,
    templates_dir: Path,
) -> str:
    """Return the pyproject.toml text for the orphan branch.

    Preference: use the caller's `worlds/<slug>/pyproject.toml` if present,
    injecting version and authors from archipelago.json when missing/blank.
    Fallback: render the bundled template.
    """
    caller_pyproject = caller_world_dir / "pyproject.toml"
    world_version = str(archipelago_json.get("world_version", "")).strip()
    authors = archipelago_json.get("authors") or []
    game_name = archipelago_json.get("game", slug)

    if caller_pyproject.is_file():
        with open(caller_pyproject, "rb") as f:
            data = tomllib.load(f)
        project = data.setdefault("project", {})
        # Inject if missing OR if the caller used the placeholder dynamic-version pattern
        if not project.get("version") and world_version:
            project["version"] = world_version
        if not project.get("authors") and authors:
            project["authors"] = [{"name": a} for a in authors]
        # Always overwrite description from archipelago.json's `game` field — keeps
        # the orphan branch's project.description in sync.
        project["description"] = f"MultiWorld: {game_name}"
        if _HAS_TOMLI_W:
            return tomli_w.dumps(data)
        # No tomli_w available — emit the original text unchanged but warn.
        print(
            "::warning::tomli_w not available; emitting caller's pyproject.toml verbatim "
            "(version/authors injection skipped). pip install tomli_w in the workflow.",
            file=sys.stderr,
        )
        return caller_pyproject.read_text(encoding="utf-8")

    # Fallback: render the bundled template.
    return render_jinja_template(
        templates_dir / "pyproject.toml.j2",
        slug=slug,
        world_version=world_version,
        game_name=game_name,
        authors=authors,
    )


def shape(
    *,
    source_root: Path,
    slug: str,
    templates_dir: Path,
    output_dir: Path,
    caller_repo: str,
    source_ref: str,
) -> None:
    caller_world_dir = source_root / "worlds" / slug
    if not caller_world_dir.is_dir():
        raise SystemExit(
            f"::error::worlds/{slug}/ not found in {source_root}. "
            f"This action requires the world's source to live at worlds/{slug}/ "
            f"in the caller repo."
        )
    archipelago_json_path = caller_world_dir / "archipelago.json"
    if not archipelago_json_path.is_file():
        raise SystemExit(
            f"::error::worlds/{slug}/archipelago.json not found. "
            f"It is required to source world_version + authors + game name."
        )
    archipelago_json = read_archipelago_json(archipelago_json_path)
    if not str(archipelago_json.get("world_version", "")).strip():
        raise SystemExit(
            "::error::archipelago.json is missing 'world_version'. "
            "The orphan branch's tag uses this; it must be set."
        )

    if output_dir.exists():
        shutil.rmtree(output_dir)
    (output_dir / "src" / "worlds").mkdir(parents=True)

    # Copy the world dir wholesale into src/worlds/<slug>/.
    shutil.copytree(caller_world_dir, output_dir / "src" / "worlds" / slug)

    pyproject_text = select_or_render_pyproject(
        caller_world_dir=caller_world_dir,
        slug=slug,
        archipelago_json=archipelago_json,
        templates_dir=templates_dir,
    )
    (output_dir / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")

    # Drop pyproject.toml from inside src/worlds/<slug>/ — the root one is canonical.
    nested_pyproject = output_dir / "src" / "worlds" / slug / "pyproject.toml"
    if nested_pyproject.is_file():
        nested_pyproject.unlink()

    # Single source of truth for downstream workflow steps. build.yml reads
    # this instead of re-parsing archipelago.json. The fields here are exactly
    # the ones build.yml needs: slug for sanity logging, world_version for the
    # tag-skew check, and game for human-readable summaries.
    shape_info = {
        "slug": slug,
        "world_version": str(archipelago_json["world_version"]).strip(),
        "game": archipelago_json.get("game", slug),
        "source_ref": source_ref,
        "caller_repo": caller_repo,
        "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (output_dir / ".shape_info.json").write_text(
        json.dumps(shape_info, indent=2) + "\n", encoding="utf-8",
    )

    print(f"shaped tree at {output_dir}")
    print(f"  slug:          {slug}")
    print(f"  world_version: {shape_info['world_version']}")
    print(f"  game:          {shape_info['game']}")
    print(f"  authors:       {archipelago_json.get('authors', [])}")


def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path,
                        help="Caller repo checkout root")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--templates-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--caller-repo", required=True,
                        help="GitHub <org>/<repo> of the caller, for the README install command")
    parser.add_argument("--source-ref", required=True,
                        help="Git ref (tag/sha/branch) the build was made from")
    args = parser.parse_args(argv)

    shape(
        source_root=args.source_root,
        slug=args.slug,
        templates_dir=args.templates_dir,
        output_dir=args.output_dir,
        caller_repo=args.caller_repo,
        source_ref=args.source_ref,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
