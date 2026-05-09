# MultiworldGG/build-and-publish-action

Reusable GitHub workflow for per-world MultiworldGG repos. On each release of a
world, this action:

1. Reads `worlds/<slug>/archipelago.json` from your repo at the release tag's
   checked-out source
2. Builds a pip-installable wheel (`<dist>-<world_version>-py3-none-any.whl`)
3. Uploads the wheel as an asset on the GitHub release

The matching `MultiworldGG-Index` PR is opened separately by the
**Oliver-Multiworld-Squirrel** GitHub App when it sees this workflow's
`workflow_run.completed` event. Karen's review checks fire automatically once
that PR is open. None of that lives in this action.

## Quick start (caller workflow)

In your per-world repo, create `.github/workflows/make_pyproject.yml`:

```yaml
name: Create and Release Python Package
on:
  release:
    types: [published]
  workflow_dispatch: {}

permissions:
  contents: write   # for `gh release upload`

jobs:
  publish:
    uses: MultiworldGG/build-and-publish-action/.github/workflows/build.yml@v3
    # No `with:` needed for single-world repos.
    # No `secrets:` — no Oliver secrets needed.
```

## Slug resolution

The action resolves the world slug in this order:

1. **`vars.WORLD_FOLDER_NAME`** (single-world repos): set in
   Settings → Secrets and variables → Actions → Variables →
   `WORLD_FOLDER_NAME=<slug>` (e.g. `WORLD_FOLDER_NAME=clique`). Your release
   tag can then be anything (`v1.0.0`, `release-2026-05-08`, etc.).

2. **Release tag prefix `<slug>-<world_version>`** (multi-world repos): if
   `WORLD_FOLDER_NAME` is unset, the action requires the release event and
   parses the slug from the tag. Tag your release as e.g. `mariolands-1.2.3`
   to publish `worlds/mariolands/` at `1.2.3`. This is the recommended path
   for repos that ship multiple worlds out of one repo (such as
   [TheLX5/Archipelago](https://github.com/TheLX5/Archipelago/)).

The Oliver-Multiworld-Squirrel GitHub App must also be installed on your repo
(read-only) so it can see the `workflow_run.completed` event and open the
Index PR on your behalf. Install it from
<https://github.com/apps/oliver-multiworld-squirrel>.

## Repo layout requirement

Your world's source must live at `worlds/<slug>/` in your repo.
`archipelago.json` is required at `worlds/<slug>/archipelago.json` and must
include `world_version`.

If you ship `worlds/<slug>/pyproject.toml`, it is used as-is — `version`,
`authors`, and `description` are injected from `archipelago.json` only when
missing. Otherwise a minimal default is generated.

## Inputs

| name | required | default | notes |
|---|---|---|---|
| `source-ref` | | release tag, else `github.sha` | What to check out from your repo. |
| `dry-run` | | `false` | Shape + build the wheel but skip the release-asset upload. |

## Output

A single `.whl` file attached to the GitHub release as an asset. Its URL has
the form:

```
https://github.com/<owner>/<repo>/releases/download/<release_tag>/<dist>-<world_version>-py3-none-any.whl
```

This URL is what the Index manifest's `module_location` will pin to. Pip can
install it directly: `pip install <url>`.

## Versioning constraint

The release tag is the immutability boundary. GitHub does not silently allow
re-publishing a release tag at a different SHA — you must manually delete and
re-create the release to do so. The action uses `gh release upload --clobber`
so re-running the workflow on the **same** release replaces the asset bytes
without changing the tag SHA; this is intentional for fixing a transient
build failure on a release that already exists.

To publish a new build, **bump `world_version` in `archipelago.json`**, tag a
new release, and let the workflow run.

## Pinning the action

Pin to a major-version tag (`@v3`); patch updates fast-forward `v3`. Breaking
changes cut a new major. Pin to a SHA (`@<full-sha>`) for full reproducibility.

## Migration from v2 (orphan branch + tag)

v2 force-pushed a `wheel/worlds/<slug>` branch and a
`wheel/worlds/<slug>/<world_version>` tag on your repo. v3 does neither — the
wheel is a release asset instead.

Existing `wheel/worlds/<slug>` branches and tags from v2 are left untouched on
already-onboarded repos; v3 simply stops creating new ones. The Index manifest
gets rewritten to the asset-URL form on the world's next release.

## Layout in this action repo

```
.github/workflows/build.yml      reusable workflow (workflow_call)
scripts/
  shape_orphan.py                build the temp orphan tree from caller's worlds/<slug>/
templates/
  pyproject.toml.j2              fallback per-world pyproject (only used when caller has none)
  README.md.j2                   auto-generated README that ships inside the wheel
```

## License

MIT (see `LICENSE`).
