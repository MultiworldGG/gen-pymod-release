from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class WorldManifest:
    apworld: str
    game: str
    world_version: str | None
    repo_url: str | None
    path: Path
    data: dict[str, Any]


@dataclass(frozen=True)
class GitHubRelease:
    url: str
    is_draft: bool
    assets: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssetSummary:
    wheel: str
    apworld: str | None


VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def main() -> int:
    args = _parse_args()
    _apply_config(args)

    repo_root = _find_repo_root(Path.cwd())
    manifest = _load_manifest(repo_root, args.apworld)
    repo = _resolve_repo(repo_root, args.repo, manifest.repo_url, args.remote, input)
    branch = args.branch or _current_branch(repo_root)
    existing_versions = _existing_versions(repo_root, repo, args.remote, manifest.apworld)
    target_version = _resolve_target_version(
        current_version=manifest.world_version,
        requested_version=args.version,
        existing_versions=existing_versions,
        input_func=input,
    )
    manifest = _write_manifest_updates(manifest, repo=repo, version=target_version, dry_run=args.dry_run)

    if _manifest_has_git_changes(repo_root, manifest.path):
        _commit_manifest(repo_root, manifest, args.commit, args.dry_run)

    tag = f"{manifest.apworld}-{target_version}"
    source_ref = tag

    _ensure_clean_worktree(repo_root, args.allow_dirty, args.dry_run)
    _ensure_local_tag_is_available(repo_root, tag)
    _create_local_tag(repo_root, tag, args.dry_run)
    _push_current_branch(repo_root, args.remote, branch, args.dry_run)
    _push_tag(repo_root, args.remote, tag, args.dry_run)

    release_url = _ensure_draft_release(repo_root, tag, manifest, repo, args.notes, args.dry_run)

    if not args.dry_run:
        run_id = _dispatch_workflow(repo_root, repo, args.workflow, branch, tag, source_ref, manifest.apworld)
        _wait_for_workflow(repo_root, repo, run_id, args.timeout, args.poll_interval)
        summary = _verify_release_assets(repo_root, repo, tag)
        print(f"Verified release assets: wheel={summary.wheel}, apworld={summary.apworld or '(none)'}")
    else:
        _print_dry_run_dispatch(repo, args.workflow, branch, tag, source_ref, manifest.apworld)

    print()
    print(f"Draft release ready: {release_url}")
    print("Review the attached assets in GitHub, then click Publish.")
    print("The wheel upload remains non-clobbering; delete/recreate assets deliberately if a rerun is needed.")

    if args.open:
        webbrowser.open(release_url)

    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare a draft APWorld release, dispatch packaging, and verify "
            "assets before publication. Run this from the root of your world repo."
        )
    )
    parser.add_argument(
        "--version",
        help=(
            "World version to publish. Defaults to the next patch after the "
            "latest matching release/tag."
        ),
    )
    parser.add_argument(
        "--commit",
        type=Path,
        help="Commit message file for manifest metadata changes. A template is generated when omitted.",
    )
    parser.add_argument(
        "--notes",
        type=Path,
        help="Release notes markdown file. A short draft note is generated when omitted.",
    )
    parser.add_argument(
        "--repo",
        help="GitHub repo for gh, e.g. owner/name. Defaults to archipelago.json repo_url, git remote, or gh.",
    )
    parser.add_argument(
        "--branch",
        help="Caller repo branch containing the workflow to dispatch. Defaults to the current branch.",
    )
    parser.add_argument(
        "--apworld",
        help=(
            "World folder under worlds/. Required when the repo contains more "
            "than one world folder."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Optional JSON defaults file. CLI arguments win over config values.",
    )
    parser.add_argument(
        "--remote",
        default=None,
        help="Git remote to push the current branch and release tag to.",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the draft release URL in the default browser.",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow creating the release tag even when the working tree is dirty.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print mutating git/gh operations without creating tags, releases, dispatches, or commits.",
    )
    parser.add_argument(
        "--workflow",
        default=None,
        help="Caller workflow file/name to dispatch after the draft release exists.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Maximum seconds to wait for the dispatched workflow.",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=15,
        help="Seconds between gh run status checks.",
    )
    return parser.parse_args()


def _apply_config(args: argparse.Namespace) -> None:
    if args.config is None:
        return

    with args.config.open(encoding="utf-8") as config_file:
        config = json.load(config_file)
    if not isinstance(config, dict):
        raise SystemExit(f"{args.config} must contain a JSON object.")

    for key in ("version", "repo", "branch", "apworld", "remote", "workflow"):
        if getattr(args, key) in (None, "") and key in config:
            setattr(args, key, str(config[key]))

    for key in ("commit", "notes"):
        if getattr(args, key) is None and key in config:
            setattr(args, key, Path(str(config[key])))

    if args.remote is None:
        args.remote = "origin"
    if args.workflow is None:
        args.workflow = "make_pyproject.yml"


def _find_repo_root(start: Path) -> Path:
    result = _run(["git", "rev-parse", "--show-toplevel"], cwd=start)
    return Path(result.stdout.strip())


def _load_manifest(repo_root: Path, apworld: str | None) -> WorldManifest:
    worlds_dir = repo_root / "worlds"
    if not worlds_dir.is_dir():
        raise SystemExit(f"Expected {worlds_dir} to exist.")

    if apworld is None:
        candidates = sorted(
            path.name
            for path in worlds_dir.iterdir()
            if path.is_dir() and (path / "archipelago.json").is_file()
        )
        if len(candidates) != 1:
            raise SystemExit(
                "Could not auto-detect a single world folder. "
                f"Pass --apworld explicitly. Candidates: {', '.join(candidates) or '(none)'}"
            )
        apworld = candidates[0]

    manifest_path = worlds_dir / apworld / "archipelago.json"
    if not manifest_path.is_file():
        raise SystemExit(f"Expected {manifest_path} to exist.")

    with manifest_path.open(encoding="utf-8") as manifest_file:
        data = json.load(manifest_file)

    game = str(data.get("game", "")).strip()
    raw_version = data.get("world_version")
    world_version = str(raw_version).strip() if raw_version is not None else None
    repo_url = str(data.get("repo_url", "")).strip() or None
    if not game:
        raise SystemExit(f"{manifest_path} is missing required field: game")

    return WorldManifest(
        apworld=apworld,
        game=game,
        world_version=world_version or None,
        repo_url=repo_url,
        path=manifest_path,
        data=data,
    )


def _resolve_repo(
    repo_root: Path,
    repo_arg: str | None,
    manifest_repo_url: str | None,
    remote: str,
    input_func: Callable[[str], str],
) -> str:
    if repo_arg:
        repo = _normalize_repo_slug(repo_arg)
        if repo:
            return repo
        raise SystemExit(f"Could not parse --repo as a GitHub repo: {repo_arg}")

    candidates = _dedupe_repo_candidates(
        [
            ("archipelago.json repo_url", _normalize_repo_slug(manifest_repo_url) if manifest_repo_url else None),
            (f"git remote {remote}", _repo_from_remote(repo_root, remote)),
            ("gh repo view", _repo_from_gh(repo_root)),
        ]
    )
    if len(candidates) == 1:
        return candidates[0][1]
    if len(candidates) > 1:
        return _prompt_repo_choice(candidates, input_func)
    raise SystemExit(
        "Could not resolve GitHub repo. Pass --repo owner/name or set repo_url in worlds/<apworld>/archipelago.json."
    )


def _dedupe_repo_candidates(candidates: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    resolved: list[tuple[str, str]] = []
    for source, repo in candidates:
        if repo and repo not in seen:
            resolved.append((source, repo))
            seen.add(repo)
    return resolved


def _prompt_repo_choice(candidates: list[tuple[str, str]], input_func: Callable[[str], str]) -> str:
    print("Multiple GitHub repos were detected:")
    for index, (source, repo) in enumerate(candidates, start=1):
        print(f"  {index}. {repo} ({source})")

    try:
        answer = input_func("Choose repo: ").strip()
    except EOFError as exc:
        raise SystemExit("Multiple repos detected; pass --repo owner/name.") from exc

    if answer.isdigit() and 1 <= int(answer) <= len(candidates):
        return candidates[int(answer) - 1][1]

    repo = _normalize_repo_slug(answer)
    if repo:
        return repo
    raise SystemExit(f"Invalid repo choice: {answer}")


def _normalize_repo_slug(value: str | None) -> str | None:
    if not value:
        return None

    candidate = value.strip()
    if not candidate:
        return None

    if re.fullmatch(r"[\w.-]+/[\w.-]+", candidate):
        return candidate.removesuffix(".git")

    patterns = (
        r"^https://github\.com/([^/]+)/([^/#]+?)(?:\.git)?/?$",
        r"^git@github\.com:([^/]+)/([^/#]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/([^/]+)/([^/#]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.match(pattern, candidate)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return None


def _repo_from_remote(repo_root: Path, remote: str) -> str | None:
    result = subprocess.run(
        ["git", "remote", "get-url", remote],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return _normalize_repo_slug(result.stdout.strip())


def _repo_from_gh(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return _normalize_repo_slug(result.stdout.strip())


def _current_branch(repo_root: Path) -> str:
    branch = _run(["git", "branch", "--show-current"], cwd=repo_root).stdout.strip()
    if not branch:
        raise SystemExit("Detached HEAD; pass --branch so the helper can dispatch a caller workflow.")
    return branch


def _existing_versions(repo_root: Path, repo: str, remote: str, apworld: str) -> set[str]:
    versions: set[str] = set()

    local_tags = subprocess.run(
        ["git", "tag", "--list", f"{apworld}-*"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if local_tags.returncode == 0:
        versions.update(_versions_from_tags(apworld, local_tags.stdout.splitlines()))

    remote_tags = subprocess.run(
        ["git", "ls-remote", "--tags", remote, f"{apworld}-*"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if remote_tags.returncode == 0:
        tags = [line.rsplit("/", 1)[-1].removesuffix("^{}") for line in remote_tags.stdout.splitlines()]
        versions.update(_versions_from_tags(apworld, tags))

    try:
        releases = subprocess.run(
            ["gh", "release", "list", "--repo", repo, "--limit", "100", "--json", "tagName"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        releases = None
    if releases is not None and releases.returncode == 0:
        data = json.loads(releases.stdout or "[]")
        versions.update(_versions_from_tags(apworld, [str(item.get("tagName", "")) for item in data]))

    return versions


def _versions_from_tags(apworld: str, tags: list[str]) -> set[str]:
    prefix = f"{apworld}-"
    return {tag.removeprefix(prefix) for tag in tags if tag.startswith(prefix) and tag != prefix}


def _resolve_target_version(
    *,
    current_version: str | None,
    requested_version: str | None,
    existing_versions: set[str],
    input_func: Callable[[str], str],
) -> str:
    default_version = requested_version or _next_patch_version(existing_versions) or current_version or "0.1.0"

    if current_version is None:
        print(f"archipelago.json has no world_version; using {default_version}.")
        return default_version

    if current_version in existing_versions:
        bump = _next_patch_version(existing_versions | {current_version}) or default_version
        return _prompt_version_choice(
            reason=f"world_version {current_version} already has a release/tag.",
            choices=[bump, current_version],
            default=bump,
            input_func=input_func,
        )

    if requested_version and current_version != requested_version:
        choices = [requested_version, current_version]
        comparison = _compare_versions(current_version, requested_version)
        if comparison is None:
            reason = f"world_version {current_version} differs from requested {requested_version}."
        elif comparison < 0:
            reason = f"world_version {current_version} is lower than requested {requested_version}."
        else:
            reason = f"world_version {current_version} is higher than requested {requested_version}."
        return _prompt_version_choice(reason=reason, choices=choices, default=requested_version, input_func=input_func)

    if requested_version is None and default_version != current_version:
        comparison = _compare_versions(current_version, default_version)
        if comparison is None:
            reason = f"world_version {current_version} differs from default {default_version}."
        elif comparison < 0:
            reason = f"world_version {current_version} is lower than default {default_version}."
        else:
            reason = f"world_version {current_version} is higher than default {default_version}."
        return _prompt_version_choice(
            reason=reason,
            choices=[default_version, current_version],
            default=default_version,
            input_func=input_func,
        )

    return current_version


def _prompt_version_choice(
    *,
    reason: str,
    choices: list[str],
    default: str,
    input_func: Callable[[str], str],
) -> str:
    print(reason)
    for index, choice in enumerate(choices, start=1):
        suffix = " [default]" if choice == default else ""
        print(f"  {index}. {choice}{suffix}")

    try:
        answer = input_func("Choose release version: ").strip()
    except EOFError:
        return default

    if not answer:
        return default
    if answer in choices:
        return answer
    if answer.isdigit() and 1 <= int(answer) <= len(choices):
        return choices[int(answer) - 1]
    raise SystemExit(f"Invalid version choice: {answer}")


def _next_patch_version(versions: set[str]) -> str | None:
    parsed = sorted((_parse_version(version), version) for version in versions if _parse_version(version) is not None)
    if not parsed:
        return None
    major, minor, patch = parsed[-1][0]
    return f"{major}.{minor}.{patch + 1}"


def _parse_version(version: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.fullmatch(version.strip())
    if not match:
        return None
    return tuple(int(group) for group in match.groups())


def _compare_versions(left: str, right: str) -> int | None:
    left_version = _parse_version(left)
    right_version = _parse_version(right)
    if left_version is None or right_version is None:
        return None
    return (left_version > right_version) - (left_version < right_version)


def _write_manifest_updates(manifest: WorldManifest, *, repo: str, version: str, dry_run: bool) -> WorldManifest:
    data = dict(manifest.data)
    repo_url = f"https://github.com/{repo}"
    changed = False

    if data.get("world_version") != version:
        data["world_version"] = version
        changed = True
    if data.get("repo_url") != repo_url:
        data["repo_url"] = repo_url
        changed = True

    updated = WorldManifest(
        apworld=manifest.apworld,
        game=manifest.game,
        world_version=version,
        repo_url=repo_url,
        path=manifest.path,
        data=data,
    )
    if not changed:
        return updated

    if dry_run:
        print(f"Would update {manifest.path}: world_version={version}, repo_url={repo_url}")
        return updated

    manifest.path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Updated {manifest.path}.")
    return updated


def _manifest_has_git_changes(repo_root: Path, manifest_path: Path) -> bool:
    rel_path = manifest_path.relative_to(repo_root)
    result = _run(["git", "status", "--porcelain", "--", str(rel_path)], cwd=repo_root)
    return bool(result.stdout.strip())


def _commit_manifest(repo_root: Path, manifest: WorldManifest, commit_path: Path | None, dry_run: bool) -> None:
    message = _commit_message(manifest, commit_path)
    rel_path = manifest.path.relative_to(repo_root)
    if dry_run:
        print(f"Would commit {rel_path} with message:\n{message}")
        return

    _run(["git", "add", "--", str(rel_path)], cwd=repo_root)
    _run(["git", "commit", "-m", message, "--", str(rel_path)], cwd=repo_root)
    print(f"Committed release metadata for {manifest.apworld}.")


def _commit_message(manifest: WorldManifest, commit_path: Path | None) -> str:
    if commit_path is not None:
        message = commit_path.read_text(encoding="utf-8").strip()
        if not message:
            raise SystemExit(f"{commit_path} is empty.")
        return message

    return (
        f"Prepare {manifest.apworld} {manifest.world_version} release\n\n"
        f"Set release metadata for {manifest.game} before packaging."
    )


def _ensure_clean_worktree(repo_root: Path, allow_dirty: bool, dry_run: bool) -> None:
    status = _run(["git", "status", "--porcelain"], cwd=repo_root).stdout.strip()
    if status and not allow_dirty and not dry_run:
        raise SystemExit(
            "Working tree has uncommitted changes. Commit the release contents "
            "first, or pass --allow-dirty if you know the tag should ignore them."
        )


def _ensure_local_tag_is_available(repo_root: Path, tag: str) -> None:
    current_commit = _run(["git", "rev-parse", "HEAD"], cwd=repo_root).stdout.strip()
    local = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if local.returncode == 0 and local.stdout.strip() != current_commit:
        raise SystemExit(f"Local tag {tag!r} already exists at a different commit.")


def _create_local_tag(repo_root: Path, tag: str, dry_run: bool) -> None:
    local = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if local.returncode == 0:
        print(f"Local tag {tag} already points at HEAD.")
        return

    if dry_run:
        print(f"Would create local tag {tag}.")
        return

    _run(["git", "tag", tag], cwd=repo_root)
    print(f"Created local tag {tag}.")


def _push_current_branch(repo_root: Path, remote: str, branch: str, dry_run: bool) -> None:
    if dry_run:
        print(f"Would push current HEAD to {remote}/{branch}.")
        return

    print(f"Pushing current branch {branch} to {remote}.")
    _run(["git", "push", remote, f"HEAD:{branch}"], cwd=repo_root)


def _push_tag(repo_root: Path, remote: str, tag: str, dry_run: bool) -> None:
    if dry_run:
        print(f"Would push tag {tag} to {remote}.")
        return

    print(f"Pushing tag {tag} to {remote}.")
    _run(["git", "push", remote, tag], cwd=repo_root)


def _ensure_draft_release(
    repo_root: Path,
    tag: str,
    manifest: WorldManifest,
    repo: str,
    notes_path: Path | None,
    dry_run: bool,
) -> str:
    existing = _gh_release(repo_root, tag, repo)
    if existing:
        if not existing.is_draft:
            raise SystemExit(
                f"Release {tag} already exists and is published. "
                "Bump world_version before publishing another build."
            )
        print(f"Release {tag} already exists.")
        return existing.url

    notes = _release_notes(manifest, notes_path)
    if dry_run:
        print(f"Would create draft release {tag} in {repo}.")
        return f"https://github.com/{repo}/releases/tag/{tag}"

    cmd = [
        "gh",
        "release",
        "create",
        tag,
        "--draft",
        "--verify-tag",
        "--title",
        f"{manifest.game} {manifest.world_version}",
        "--notes",
        notes,
    ]
    cmd.extend(["--repo", repo])

    result = _run(cmd, cwd=repo_root)
    release_url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if release_url.startswith("http"):
        return release_url

    fallback = _gh_release(repo_root, tag, repo)
    if fallback:
        return fallback.url
    raise SystemExit("Draft release was created, but gh did not return a URL.")


def _release_notes(manifest: WorldManifest, notes_path: Path | None) -> str:
    if notes_path is not None:
        notes = notes_path.read_text(encoding="utf-8").strip()
        if not notes:
            raise SystemExit(f"{notes_path} is empty.")
        return notes

    return (
        f"Draft release for {manifest.game} {manifest.world_version}.\n\n"
        "Packaging is dispatched before publication. Publish this release only "
        "after the wheel and optional .apworld assets are attached."
    )


def _gh_release(repo_root: Path, tag: str, repo: str) -> GitHubRelease | None:
    cmd = ["gh", "release", "view", tag, "--json", "url,isDraft,assets", "--repo", repo]

    try:
        result = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout)
    release_url = str(data.get("url", "")).strip()
    if not release_url:
        return None
    assets = tuple(str(asset.get("name", "")) for asset in data.get("assets", []) if asset.get("name"))
    return GitHubRelease(url=release_url, is_draft=bool(data.get("isDraft")), assets=assets)


def _dispatch_workflow(
    repo_root: Path,
    repo: str,
    workflow: str,
    branch: str,
    tag: str,
    source_ref: str,
    apworld: str,
) -> int:
    print(f"Dispatching {workflow} on {branch} for draft release {tag}.")
    _run(
        [
            "gh",
            "workflow",
            "run",
            workflow,
            "--repo",
            repo,
            "--ref",
            branch,
            "-f",
            f"release-tag={tag}",
            "-f",
            f"source-ref={source_ref}",
            "-f",
            f"apworld={apworld}",
            "-f",
            "dry-run=false",
        ],
        cwd=repo_root,
    )
    time.sleep(5)
    result = _run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            workflow,
            "--branch",
            branch,
            "--event",
            "workflow_dispatch",
            "--limit",
            "1",
            "--json",
            "databaseId",
        ],
        cwd=repo_root,
    )
    runs = json.loads(result.stdout or "[]")
    if not runs:
        raise SystemExit(f"Workflow {workflow} was dispatched, but no workflow_dispatch run was found.")
    run_id = int(runs[0]["databaseId"])
    print(f"Dispatched workflow run: {run_id}")
    return run_id


def _wait_for_workflow(repo_root: Path, repo: str, run_id: int, timeout: int, poll_interval: int) -> None:
    deadline = time.monotonic() + timeout
    while True:
        result = _run(
            ["gh", "run", "view", str(run_id), "--repo", repo, "--json", "status,conclusion,url"],
            cwd=repo_root,
        )
        data = json.loads(result.stdout)
        status = str(data.get("status", ""))
        conclusion = str(data.get("conclusion", ""))
        url = str(data.get("url", ""))
        print(f"Workflow run {run_id}: status={status}, conclusion={conclusion or '(pending)'}")

        if status == "completed":
            if conclusion == "success":
                return
            raise SystemExit(f"Workflow run {run_id} failed with conclusion={conclusion}: {url}")

        if time.monotonic() >= deadline:
            raise SystemExit(f"Timed out waiting for workflow run {run_id}: {url}")
        time.sleep(poll_interval)


def _verify_release_assets(repo_root: Path, repo: str, tag: str) -> AssetSummary:
    release = _gh_release(repo_root, tag, repo)
    if release is None:
        raise SystemExit(f"Release {tag} does not exist.")
    if not release.is_draft:
        raise SystemExit(f"Release {tag} is already published; expected a draft before human publication.")
    return _validate_release_assets(release.assets)


def _validate_release_assets(assets: tuple[str, ...]) -> AssetSummary:
    wheels = [asset for asset in assets if asset.endswith(".whl")]
    apworlds = [asset for asset in assets if asset.endswith(".apworld")]
    if len(wheels) != 1:
        raise SystemExit(f"Expected exactly one .whl asset on the draft release, found {len(wheels)}.")
    if len(apworlds) > 1:
        raise SystemExit(f"Expected zero or one .apworld asset on the draft release, found {len(apworlds)}.")
    return AssetSummary(wheel=wheels[0], apworld=apworlds[0] if apworlds else None)


def _print_dry_run_dispatch(repo: str, workflow: str, branch: str, tag: str, source_ref: str, apworld: str) -> None:
    command = [
        "gh",
        "workflow",
        "run",
        workflow,
        "--repo",
        repo,
        "--ref",
        branch,
        "-f",
        f"release-tag={tag}",
        "-f",
        f"source-ref={source_ref}",
        "-f",
        f"apworld={apworld}",
        "-f",
        "dry-run=false",
    ]
    print("Would dispatch workflow:")
    print(" ".join(shlex.quote(part) for part in command))


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise SystemExit(f"{' '.join(command)} failed:\n{message}") from exc


if __name__ == "__main__":
    sys.exit(main())
