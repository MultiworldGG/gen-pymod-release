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
import re
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


_LAUNCH_DISPATCH_NAMES = frozenset({"launch", "launch_subprocess", "launch_component"})
# Raw multiprocessing dispatch: `Process(target=fn)` / `multiprocessing.Process(target=fn)`.
# The target callable is the `target=` keyword rather than the first positional arg.
_PROCESS_DISPATCH_NAMES = frozenset({"Process"})


def _extract_inner_target(
    expr: ast.expr, body_imports: dict[str, tuple[str, str]]
) -> Optional[tuple[str, str]]:
    """Resolve a dispatch argument expression to the inner client `(module, attr)`,
    using the names an inner `from .X import Y` bound in the wrapper body.

    Handles the shapes worlds use to hand a client callable to the launcher:
      - `Y`                  bare name (e.g. `launch(Y, ...)`, `Process(target=Y)`)
      - `mod.Y`              attribute on a `from . import mod` module import
      - `Y(*args)`           the callee itself is the client (ufo50)
      - `partial(Y, *args)`  partial-bound client (xenobladex)

    The attribute branch assumes the base name is a *module* import
    (`from . import client; client.launch`); the `from .X import Y; Y.attr`
    shape doesn't occur in practice and would mis-resolve here.
    """
    if isinstance(expr, ast.Name):
        return body_imports.get(expr.id)
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        bound = body_imports.get(expr.value.id)
        if bound is not None:
            module, name = bound
            return (f"{module}.{name}", expr.attr)
        return None
    if isinstance(expr, ast.Call):
        # `launch(*args)` — the callee itself is the inner client.
        from_callee = _extract_inner_target(expr.func, body_imports)
        if from_callee is not None:
            return from_callee
        # `partial(launch, *args)` — the first resolvable positional is the client.
        for arg in expr.args:
            resolved = _extract_inner_target(arg, body_imports)
            if resolved is not None:
                return resolved
    return None


def _trace_wrapper_to_inner_target(
    func_def: ast.FunctionDef | ast.AsyncFunctionDef,
    file_module: str,
    is_init: bool,
) -> Optional[tuple[str, str]]:
    """Trace a thin launcher wrapper to the inner client module + attr.

    Worlds typically register their client via a wrapper sitting in
    `__init__.py`:

        def launch_client(*args):
            from .Client import main
            launch(main, name="...", args=args)

    Pointing the entry point at this wrapper would force the entry-point
    consumer to import `__init__.py`, which drags in `BaseClasses`, the
    world's option/region/item module-level code, and re-fires the
    `components.append(...)` side effect we're trying to retire. Instead,
    walk the wrapper's body for the inner `from .X import Y` and the inner
    dispatch call, then emit `<resolved>:Y` so the entry point lands on the
    actual client module. Recognized dispatch calls are
    `launch(...)` / `launch_subprocess(...)` / `launch_component(...)` (target
    is the first positional arg) and `Process(target=...)` /
    `multiprocessing.Process(target=...)` (target is the `target` keyword).
    The target expression is unwrapped by `_extract_inner_target`, which also
    handles `mod.Y`, `Y(*args)`, and `partial(Y, *args)` shapes.

    Returns None when the wrapper is not a recognized `import + dispatch(Y)`
    shape — in that case the caller should skip emission and warn.
    """
    body_imports: dict[str, tuple[str, str]] = {}
    for stmt in func_def.body:
        if not isinstance(stmt, ast.ImportFrom):
            continue
        if stmt.level >= 1:
            target_module = _resolve_relative_import(stmt, file_module, is_init)
        elif stmt.module:
            target_module = stmt.module
        else:
            continue
        for alias in stmt.names:
            if alias.name == "*":
                continue
            body_imports[alias.asname or alias.name] = (target_module, alias.name)

    if not body_imports:
        return None

    for stmt in ast.walk(func_def):
        if not isinstance(stmt, ast.Call):
            continue
        call_name: Optional[str] = None
        if isinstance(stmt.func, ast.Name):
            call_name = stmt.func.id
        elif isinstance(stmt.func, ast.Attribute):
            call_name = stmt.func.attr

        if call_name in _LAUNCH_DISPATCH_NAMES:
            target_expr: Optional[ast.expr] = stmt.args[0] if stmt.args else None
        elif call_name in _PROCESS_DISPATCH_NAMES:
            target_expr = next(
                (kw.value for kw in stmt.keywords if kw.arg == "target"), None
            )
        else:
            continue
        if target_expr is None:
            continue

        resolved = _extract_inner_target(target_expr, body_imports)
        if resolved is not None:
            return resolved

    return None


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

    Key convention matches `tools/add_required_world_files.py` from the
    monorepo: the first discovered client is keyed `worlds.<apworld>.Client`;
    additional clients (rare) are keyed `worlds.<apworld>.Client.<func_name>`
    so they cannot collide with the canonical one.

    Wrappers that live in `__init__.py` are traced one level deeper via
    `_trace_wrapper_to_inner_target`, because pointing the entry point at the
    package's `__init__` would defeat the whole purpose of using entry-point
    discovery (it would re-import all of the heavy `BaseClasses` /
    options / regions module-level code AND re-fire `components.append`).
    If the wrapper isn't a simple `import + launch(...)` shape we skip with
    a loud warning rather than emit something that won't load.

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

    pkg_root = f"worlds.{apworld}"
    results: list[tuple[str, str]] = []
    seen_targets: set[tuple[str, str]] = set()

    for py_file in sorted(world_dir.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            logging.warning("Skipping %s during client-entry-point scan: %s", py_file, exc)
            continue

        file_module = _file_module_path(py_file, world_dir, apworld)
        is_init = py_file.name == "__init__.py"

        # Local names that refer to LauncherComponents.Type. It's frequently
        # aliased on import (e.g. `from worlds.LauncherComponents import
        # Type as ComponentType`), so `component_type=ComponentType.CLIENT`
        # is the same as `Type.CLIENT`. Seed with the canonical name so plain
        # `Type.CLIENT` always matches; collect aliases only from
        # LauncherComponents imports to avoid e.g. `typing.Type` false matches.
        type_aliases: set[str] = {"Type"}
        for node in ast.walk(tree):
            if (isinstance(node, ast.ImportFrom) and node.module
                    and node.module.split(".")[-1] == "LauncherComponents"):
                for alias in node.names:
                    if alias.name == "Type":
                        type_aliases.add(alias.asname or "Type")

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
                            and isinstance(v.value, ast.Name)
                            and v.value.id in type_aliases):
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

            # Avoid pointing the entry point at the world's __init__ module.
            # Loading it would re-fire all the heavy __init__-level imports
            # (BaseClasses, options, regions, ...) and re-run
            # `components.append(...)`. Trace the wrapper body to the real
            # client module instead.
            if target_module == pkg_root:
                # Walk the whole tree (not just tree.body) so wrappers nested
                # inside `if`/`try` blocks are found — e.g. dk64's
                # `if baseclasses_loaded:` and stardew_valley's tracker guards.
                wrapper_def = next(
                    (n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == func_name),
                    None,
                )
                traced: Optional[tuple[str, str]] = (
                    _trace_wrapper_to_inner_target(wrapper_def, file_module, is_init)
                    if wrapper_def is not None else None
                )
                if traced is None:
                    logging.warning(
                        "%s: Type.CLIENT Component(func=%s) resolves to %s "
                        "(the world's __init__), and the wrapper body isn't a "
                        "simple `from .X import Y; launch(Y, ...)` shape. "
                        "Skipping entry-point emission — pointing it at "
                        "__init__ would defeat lazy client loading. Move %s "
                        "into a sibling module (e.g. Register.py / "
                        "client/component.py) or pin a custom entry point in "
                        "the world's pyproject.toml.",
                        py_file, func_name, target_module, func_name,
                    )
                    continue
                target_module, attr = traced

            if (target_module, attr) in seen_targets:
                continue
            seen_targets.add((target_module, attr))

            # Match the convention in
            # `MultiworldGG-gui-changes/tools/add_required_world_files.py`:
            # the first client per world is keyed `worlds.<apworld>.Client`
            # (the canonical singular). For the rare case of multiple
            # clients in one world, disambiguate with a function-name
            # suffix so neither collides with the canonical key.
            if not results:
                ep_key = f"{pkg_root}.Client"
            else:
                ep_key = f"{pkg_root}.Client.{func_name}"
            ep_value = f"{target_module}:{attr}"
            results.append((ep_key, ep_value))

    return results


# pip-only directive flags (anything after these on the line is not a PEP 508 spec).
# Source: https://pip.pypa.io/en/stable/reference/requirements-file-format/
_PIP_DIRECTIVE_FLAGS_SKIP = frozenset({
    "-r", "--requirement",
    "-c", "--constraint",
    "-e", "--editable",
    "-i", "--index-url",
    "--extra-index-url",
    "--find-links",
    "--no-index",
    "--no-binary",
    "--only-binary",
    "--pre",
    "--trusted-host",
    "--use-feature",
})
_PIP_DIRECTIVE_FLAGS_WARN = frozenset({"-r", "--requirement", "-c", "--constraint",
                                        "-e", "--editable"})
# Per-line pip options that follow the requirement spec (split off, keep the spec).
_PIP_OPTION_PREFIXES = ("--hash=", "--global-option=", "--config-settings=",
                        "--install-option=")


def _canonical_dist_name(spec: str) -> str:
    """Extract and PEP 503-normalize the distribution name from a PEP 508 spec.

    Stdlib only — does not pull in `packaging`. Handles `pkg`, `pkg[extra]`,
    `pkg==1`, `pkg>=1,<2`, `pkg @ url`, `pkg ; marker`. Returns "" if the
    spec doesn't start with an identifier.
    """
    s = spec.strip()
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", s)
    if not m:
        return ""
    return re.sub(r"[-_.]+", "-", m.group(1)).lower()


def _has_setuptools(deps: list[str]) -> bool:
    return any(_canonical_dist_name(d) == "setuptools" for d in deps)


def parse_requirements_txt(path: Path) -> list[str]:
    """Parse a pip `requirements.txt` into a list of PEP 508 spec strings.

    Strips comments, joins backslash-continuation lines, drops per-line pip
    options (`--hash=`, `--global-option=`, etc.) that aren't valid PEP 508,
    and skips pip-only directive lines (`-r`, `-c`, `-e`, index/find-links
    flags). Logs `::warning::` for the directive flags an author might
    reasonably expect to work (`-r`, `-c`, `-e`) so it's not silently dropped.

    Stdlib only — does not import `pip` or `packaging`. Each returned string
    is meant to be dropped directly into pyproject `[project].dependencies`,
    which setuptools parses per PEP 621 (PEP 508 grammar including direct
    references like `pkg @ git+https://…` and env markers like
    `; python_version == '3.13'`).
    """
    text = path.read_text(encoding="utf-8")
    # Join backslash continuations into single logical lines.
    text = re.sub(r"\\\r?\n", " ", text)

    deps: list[str] = []
    for raw_line in text.splitlines():
        # Per pip's parser, `#` starts a comment only when preceded by
        # whitespace (or at column 0). A bare `#` mid-token is a URL fragment
        # — e.g. `pkg @ git+https://host/repo@sha#egg=name`.
        line = re.split(r"(?:^|\s)#", raw_line, maxsplit=1)[0].strip()
        if not line:
            continue
        first_token = line.split(None, 1)[0]
        if first_token in _PIP_DIRECTIVE_FLAGS_SKIP:
            if first_token in _PIP_DIRECTIVE_FLAGS_WARN:
                logging.warning(
                    "::warning::requirements.txt %s: skipping pip directive %r — "
                    "pyproject [project].dependencies only accepts PEP 508 specs.",
                    path, first_token,
                )
            continue
        # Split off trailing pip-only per-line options.
        # Split on whitespace; rebuild only the tokens that aren't pip options.
        kept: list[str] = []
        for tok in line.split():
            if any(tok.startswith(p) for p in _PIP_OPTION_PREFIXES):
                break  # all subsequent tokens are pip options too (hash chains)
            kept.append(tok)
        spec = " ".join(kept).strip()
        if spec:
            deps.append(spec)
    return deps


def scan_for_pkg_resources(world_dir: Path) -> bool:
    """True if any `.py` under `world_dir` imports `pkg_resources`.

    `pkg_resources` ships with setuptools but is not in the stdlib — worlds
    that import it need `setuptools` declared as a runtime dep or they
    ImportError at module load. AST-based to avoid false positives from
    comments / strings.
    """
    for py_file in world_dir.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name == "pkg_resources"
                       or alias.name.startswith("pkg_resources.")
                       for alias in node.names):
                    return True
            elif isinstance(node, ast.ImportFrom):
                if node.module == "pkg_resources" or (
                    node.module and node.module.startswith("pkg_resources.")
                ):
                    return True
    return False


def select_or_render_pyproject(
    *,
    caller_world_dir: Path,
    apworld: str,
    archipelago_json: dict,
    templates_dir: Path,
    client_entry_points: list[tuple[str, str]],
    requirements_deps: list[str],
) -> str:
    """Return the pyproject.toml text for the orphan branch.

    Preference: use the caller's `worlds/<apworld>/pyproject.toml` if present,
    injecting version and authors from archipelago.json when missing/blank.
    Fallback: render the bundled template.

    In both paths, statically-discovered `mwgg.client` entry points are
    injected only when the caller hasn't already declared its own — the
    caller's declaration always wins.

    `requirements_deps` are PEP 508 specs sourced from
    `worlds/<apworld>/requirements.txt` (plus the auto-injected `setuptools`
    when the world imports `pkg_resources`). They land in
    `[project].dependencies`. If the caller's pyproject already declares
    `[project].dependencies`, the caller wins and we emit a `::warning::`
    that requirements.txt was ignored — matches the precedence policy used
    for `version`/`authors`.
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

    def _inject_dependencies(data: dict, *, caller_has_pyproject: bool) -> None:
        if not requirements_deps:
            return
        project = data.setdefault("project", {})
        existing = project.get("dependencies")
        if caller_has_pyproject and existing:
            logging.warning(
                "::warning::%s ships pyproject.toml with [project].dependencies AND "
                "a requirements.txt. Caller's pyproject wins; requirements.txt entries "
                "(%s) are ignored. Remove one to silence this warning.",
                apworld, ", ".join(requirements_deps),
            )
            return
        project["dependencies"] = list(requirements_deps)

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
        _inject_dependencies(data, caller_has_pyproject=True)
        if _HAS_TOMLI_W:
            return tomli_w.dumps(data)
        # No tomli_w available — emit the original text unchanged but warn.
        print(
            "::warning::tomli_w not available; emitting caller's pyproject.toml verbatim "
            "(version/authors/dependencies injection skipped). pip install tomli_w in the workflow.",
            file=sys.stderr,
        )
        return caller_pyproject.read_text(encoding="utf-8")

    # Fallback: render the bundled template, then re-parse and inject entry points + deps.
    rendered = render_jinja_template(
        templates_dir / "pyproject.toml.j2",
        apworld=apworld,
        world_version=world_version,
        game_name=game_name,
        authors=authors,
    )
    needs_rewrite = (client_entry_points or requirements_deps) and _HAS_TOMLI_W
    if needs_rewrite:
        try:
            data = tomllib.loads(rendered)
        except tomllib.TOMLDecodeError as exc:
            logging.warning(
                "Could not re-parse the rendered fallback pyproject.toml to inject "
                "entry points / dependencies; the wheel may be missing them: %s", exc,
            )
            return rendered
        _inject_entry_points(data)
        _inject_dependencies(data, caller_has_pyproject=False)
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

    requirements_path = caller_world_dir / "requirements.txt"
    requirements_deps = (
        parse_requirements_txt(requirements_path)
        if requirements_path.is_file() else []
    )
    if scan_for_pkg_resources(copied_world_dir) and not _has_setuptools(requirements_deps):
        logging.warning(
            "::warning::%s imports pkg_resources but does not declare setuptools "
            "in requirements.txt. Auto-injecting setuptools into [project].dependencies. "
            "Add `setuptools` to worlds/%s/requirements.txt to silence this warning.",
            apworld, apworld,
        )
        requirements_deps.append("setuptools")

    pyproject_text = select_or_render_pyproject(
        caller_world_dir=caller_world_dir,
        apworld=apworld,
        archipelago_json=archipelago_json,
        templates_dir=templates_dir,
        client_entry_points=client_entry_points,
        requirements_deps=requirements_deps,
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
        # NOTE: pattern is *.py[co] (not *.py[cod]) — the bracket char-class
        # matches any single listed letter, so [cod] would also match `.pyd`,
        # i.e. Windows native extensions. We want to exclude .pyc/.pyo
        # bytecode but ship .pyd extensions.
        "global-exclude *~ *.py[co]\n"
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
        "dependencies": list(requirements_deps),
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
    if requirements_deps:
        print(f"  dependencies:    {len(requirements_deps)}")
        for dep in requirements_deps:
            print(f"    {dep}")
    else:
        print("  dependencies:    (none)")


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
