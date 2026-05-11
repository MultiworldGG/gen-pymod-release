"""Build the wheel-source tree for the build-and-publish-action.

Inputs:
  - source_root     : checkout of the caller repo (must contain worlds/<apworld>/)
  - apworld            : world apworld
  - templates_dir   : path to this action's templates/ directory
  - output_dir      : where to write the orphan-shaped tree

Outputs (under output_dir):
  pyproject.toml
  .shape_info.json   {apworld, world_version, game} — single source of truth
                     for downstream workflow steps; avoids re-parsing
                     archipelago.json from build.yml.
  src/
    worlds/
      <apworld>/
        ...world source...

If the caller's repo ships `worlds/<apworld>/pyproject.toml`, it is used as-is —
with `version` and `authors` injected from `archipelago.json` only if those
fields are absent (mirrors the `tools/build_wheels.py` pattern in the
MultiworldGG monorepo).

Otherwise, the bundled `templates/pyproject.toml.j2` fallback is rendered.

Pure functions; the workflow shells out to this with the right args.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import json
import logging
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


def _file_module_path(py_file: Path, world_dir: Path, apworld: str) -> str:
    """Dotted module path for a `.py` file inside the copied world tree.

    e.g. `worlds/<apworld>/client/component.py` → `worlds.<apworld>.client.component`,
         `worlds/<apworld>/__init__.py`         → `worlds.<apworld>`.
    """
    rel_parts = list(py_file.relative_to(world_dir).parts)
    if rel_parts[-1] == "__init__.py":
        rel_parts = rel_parts[:-1]
    else:
        rel_parts[-1] = rel_parts[-1][:-3]  # strip .py
    return ".".join([f"worlds.{apworld}", *rel_parts]) if rel_parts else f"worlds.{apworld}"


def _resolve_relative_import(node: ast.ImportFrom, file_module: str, is_init: bool) -> str:
    """Resolve a relative `from .X import Y` import to an absolute module path,
    given the dotted module of the file containing the import."""
    base_parts = file_module.split(".")
    # In a non-__init__ file, `from .` means the file's parent package.
    if not is_init:
        base_parts = base_parts[:-1]
    # `from .` → level 1; `from ..` → level 2, etc. Each extra dot walks up one package.
    for _ in range(node.level - 1):
        if base_parts:
            base_parts = base_parts[:-1]
    target_parts = list(base_parts)
    if node.module:
        target_parts.extend(node.module.split("."))
    return ".".join(target_parts)


def parse_client_entry_points(world_dir: Path, apworld: str) -> list[tuple[str, str]]:
    """Static-analysis pass to discover Type.CLIENT Component(...) registrations.

    Walks every `.py` under `world_dir/`, parses with `ast`, and for each call of
    the form `Component(..., func=NAME, component_type=Type.CLIENT, ...)` (or
    `Component("Foo Client", func=NAME, ...)` where Type.CLIENT is inferred from
    the display name containing "Client"), resolves NAME to a dotted
    `module:attr` target via the file's own imports and module-level defs.

    Returns a deterministic list of `(entry_point_key, entry_point_value)`
    suitable for `[project.entry-points."mwgg.client"]`.

    This is intentionally a textual / AST walk — we never import the world's
    own code, which would require the whole AP runtime + all transitive
    dependencies to be importable from the build sandbox. The companion
    function `parse_client_function` in `APContainer.py` is the runtime
    discovery analog; this is its build-time counterpart and matches the
    `worlds/AutoWorld` deprecation roadmap recorded in
    `project_split_design_decisions.md`.

    If the apworld name is not a valid Python identifier (e.g. `2048`,
    `civ_6` is fine, `2048` is not because it starts with a digit), entry
    points are silently skipped — `setuptools` rejects entry-point targets
    whose module path has a digit-led segment with `must be
    python-entrypoint-reference`. Such worlds are still importable via
    `importlib.import_module("worlds.2048")` at runtime; the launcher
    discovery for them must continue to rely on the AST walk in
    `APContainer.parse_client_function`, not on the entry point.
    """
    if not apworld.isidentifier():
        logging.warning(
            "apworld %r is not a valid Python identifier; skipping mwgg.client "
            "entry-points emission (setuptools would reject the target string).",
            apworld,
        )
        return []

    results: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for py_file in sorted(world_dir.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            logging.warning("Skipping %s during client-entry-point scan: %s", py_file, exc)
            continue

        file_module = _file_module_path(py_file, world_dir, apworld)
        is_init = py_file.name == "__init__.py"

        # local_name → (target_module, original_attr)
        local_to_target: dict[str, tuple[str, str]] = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                if node.level >= 1:
                    target_module = _resolve_relative_import(node, file_module, is_init)
                elif node.module:
                    target_module = node.module
                else:
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local_to_target[alias.asname or alias.name] = (target_module, alias.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local_to_target[node.name] = (file_module, node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        local_to_target[target.id] = (file_module, target.id)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "Component"):
                continue

            ctype_is_client = False
            ctype_explicit = False
            display_name_str: Optional[str] = None
            func_name: Optional[str] = None
            for kw in node.keywords:
                if kw.arg == "component_type":
                    ctype_explicit = True
                    v = kw.value
                    if (isinstance(v, ast.Attribute) and v.attr == "CLIENT"
                            and isinstance(v.value, ast.Name) and v.value.id == "Type"):
                        ctype_is_client = True
                elif kw.arg == "func":
                    if isinstance(kw.value, ast.Name):
                        func_name = kw.value.id
                elif kw.arg == "display_name":
                    if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        display_name_str = kw.value.value

            # display_name is also accepted as the first positional arg.
            if display_name_str is None and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    display_name_str = first.value

            # Mirror LauncherComponents.Component.__init__ type inference: if no
            # explicit component_type is given and the display name contains
            # "Client", the runtime treats it as Type.CLIENT.
            if not ctype_explicit and display_name_str and "Client" in display_name_str:
                ctype_is_client = True

            if not (ctype_is_client and func_name):
                continue

            target_module, attr = local_to_target.get(func_name, (file_module, func_name))
            ep_key = f"worlds.{apworld}.{func_name}"
            ep_value = f"{target_module}:{attr}"
            sig = (ep_key, ep_value)
            if sig in seen:
                continue
            seen.add(sig)
            results.append(sig)

    return results


def select_or_render_pyproject(
    *,
    caller_world_dir: Path,
    apworld: str,
    archipelago_json: dict,
    templates_dir: Path,
    client_entry_points: list[tuple[str, str]],
) -> str:
    """Return the pyproject.toml text for the orphan branch.

    Preference: use the caller's `worlds/<apworld>/pyproject.toml` if present,
    injecting version and authors from archipelago.json when missing/blank.
    Fallback: render the bundled template.

    In both paths, statically-discovered `mwgg.client` entry points are
    injected only when the caller hasn't already declared its own — the
    caller's declaration always wins.
    """
    caller_pyproject = caller_world_dir / "pyproject.toml"
    world_version = str(archipelago_json.get("world_version", "")).strip()
    authors = archipelago_json.get("authors") or []
    game_name = archipelago_json.get("game", apworld)

    def _inject_entry_points(data: dict) -> None:
        if not client_entry_points:
            return
        project = data.setdefault("project", {})
        entry_points = project.setdefault("entry-points", {})
        if "mwgg.client" in entry_points and entry_points["mwgg.client"]:
            return  # caller already declared their own — don't second-guess
        entry_points["mwgg.client"] = {k: v for k, v in client_entry_points}

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
        _inject_entry_points(data)
        if _HAS_TOMLI_W:
            return tomli_w.dumps(data)
        # No tomli_w available — emit the original text unchanged but warn.
        print(
            "::warning::tomli_w not available; emitting caller's pyproject.toml verbatim "
            "(version/authors injection skipped). pip install tomli_w in the workflow.",
            file=sys.stderr,
        )
        return caller_pyproject.read_text(encoding="utf-8")

    # Fallback: render the bundled template, then re-parse and inject entry points.
    rendered = render_jinja_template(
        templates_dir / "pyproject.toml.j2",
        apworld=apworld,
        world_version=world_version,
        game_name=game_name,
        authors=authors,
    )
    if client_entry_points and _HAS_TOMLI_W:
        try:
            data = tomllib.loads(rendered)
        except tomllib.TOMLDecodeError as exc:
            logging.warning(
                "Could not re-parse the rendered fallback pyproject.toml to inject "
                "mwgg.client entry points; the wheel will be missing them: %s", exc,
            )
            return rendered
        _inject_entry_points(data)
        return tomli_w.dumps(data)
    return rendered


def shape(
    *,
    source_root: Path,
    apworld: str,
    templates_dir: Path,
    output_dir: Path,
    caller_repo: str,
    source_ref: str,
) -> None:
    caller_world_dir = source_root / "worlds" / apworld
    if not caller_world_dir.is_dir():
        raise SystemExit(
            f"::error::worlds/{apworld}/ not found in {source_root}. "
            f"This action requires the world's source to live at worlds/{apworld}/ "
            f"in the caller repo."
        )
    archipelago_json_path = caller_world_dir / "archipelago.json"
    if not archipelago_json_path.is_file():
        raise SystemExit(
            f"::error::worlds/{apworld}/archipelago.json not found. "
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

    # Copy the world dir wholesale into src/worlds/<apworld>/.
    copied_world_dir = output_dir / "src" / "worlds" / apworld
    shutil.copytree(caller_world_dir, copied_world_dir)

    client_entry_points = parse_client_entry_points(copied_world_dir, apworld)

    pyproject_text = select_or_render_pyproject(
        caller_world_dir=caller_world_dir,
        apworld=apworld,
        archipelago_json=archipelago_json,
        templates_dir=templates_dir,
        client_entry_points=client_entry_points,
    )
    (output_dir / "pyproject.toml").write_text(pyproject_text, encoding="utf-8")

    # Drop pyproject.toml from inside src/worlds/<apworld>/ — the root one is canonical.
    nested_pyproject = output_dir / "src" / "worlds" / apworld / "pyproject.toml"
    if nested_pyproject.is_file():
        nested_pyproject.unlink()

    # Mirror tools/build_wheels.py in the monorepo: a per-world MANIFEST.in is
    # required so setuptools picks up every non-Python file and every nested
    # subpackage under src/worlds/<apworld>/. Without this, the wheel ships only
    # the top-level *.py files — data/, docs/, sub-packages, archipelago.json,
    # templates, images, etc. are all silently dropped.
    manifest_text = (
        "global-exclude *\n"
        f"graft src/worlds/{apworld}\n"
        "global-exclude *~ *.py[cod]\n"
        "include pyproject.toml\n"
    )
    (output_dir / "MANIFEST.in").write_text(manifest_text, encoding="utf-8")

    # Single source of truth for downstream workflow steps. build.yml reads
    # this instead of re-parsing archipelago.json. The fields here are exactly
    # the ones build.yml needs: apworld for sanity logging, world_version for the
    # tag-skew check, and game for human-readable summaries.
    shape_info = {
        "apworld": apworld,
        "world_version": str(archipelago_json["world_version"]).strip(),
        "game": archipelago_json.get("game", apworld),
        "source_ref": source_ref,
        "caller_repo": caller_repo,
        "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "client_entry_points": [
            {"name": k, "target": v} for k, v in client_entry_points
        ],
    }
    (output_dir / ".shape_info.json").write_text(
        json.dumps(shape_info, indent=2) + "\n", encoding="utf-8",
    )

    print(f"shaped tree at {output_dir}")
    print(f"  apworld:          {apworld}")
    print(f"  world_version: {shape_info['world_version']}")
    print(f"  game:          {shape_info['game']}")
    print(f"  authors:       {archipelago_json.get('authors', [])}")
    if client_entry_points:
        print(f"  mwgg.client entry points: {len(client_entry_points)}")
        for k, v in client_entry_points:
            print(f"    {k} = {v}")
    else:
        print("  mwgg.client entry points: (none found)")


def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path,
                        help="Caller repo checkout root")
    parser.add_argument("--apworld", required=True)
    parser.add_argument("--templates-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--caller-repo", required=True,
                        help="GitHub <org>/<repo> of the caller, for the README install command")
    parser.add_argument("--source-ref", required=True,
                        help="Git ref (tag/sha/branch) the build was made from")
    args = parser.parse_args(argv)

    shape(
        source_root=args.source_root,
        apworld=args.apworld,
        templates_dir=args.templates_dir,
        output_dir=args.output_dir,
        caller_repo=args.caller_repo,
        source_ref=args.source_ref,
    )
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
