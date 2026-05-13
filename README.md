# MultiworldGG/gen-pymod-release

Reusable GitHub workflow for per-world MultiworldGG repos. When you want to release a world
  this action:

1. Reads `worlds/<apworld>/archipelago.json` from your repo at the release tag's
   checked-out source
2. Builds a pip-installable wheel (`<dist>-<world_version>-py3-none-any.whl`)
3. Uploads the wheel as an asset on the GitHub release

The matching `MultiworldGG-Index` PR is opened separately by the
**Oliver-Multiworld-Squirrel** GitHub App when it sees this workflow's
`workflow_run.completed` event. Karen's review checks fire automatically once
that PR is open. None of that lives in this action.

## Quick start — per-world repo

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
    uses: MultiworldGG/gen-pymod-release/.github/workflows/build.yml@v3
    # No `with:` needed for single-world repos.
    # No `secrets:` — no Oliver secrets needed.
```

## Quick start — pure-Python client repo (flat layout)

For repos that ship a single pip-installable Python package at the repo root
(a `pyproject.toml` plus a top-level package directory — no `worlds/<apworld>/`
shape), use the sibling `build-wheel.yml` workflow. It rewrites
`pyproject.toml:[project].version` in the runner to the input version, builds
the wheel, tags `v<version>`, creates a GitHub Release, and uploads the wheel
as an asset. The committed `pyproject.toml` is **not** modified — the workflow
input is the single source of truth.

In your client repo, create `.github/workflows/release.yml`:

```yaml
name: Release wheel
on:
  workflow_dispatch:
    inputs:
      version:
        description: "Version to release (e.g. 0.1.1). Tag will be v<version>."
        required: true
        type: string
      dry-run:
        description: "Build only -- skip tag, release, and asset upload."
        required: false
        type: boolean
        default: false

permissions:
  contents: write

jobs:
  build-and-release:
    uses: MultiworldGG/gen-pymod-release/.github/workflows/build-wheel.yml@v3
    with:
      version: ${{ inputs.version }}
      dry-run: ${{ inputs.dry-run }}
```

Inputs accepted by `build-wheel.yml`:

| name | required | default | notes |
|---|---|---|---|
| `version` | yes | — | Becomes both the tag (`v<version>`) and the wheel's metadata version. |
| `source-ref` | | `github.sha` | Caller-repo ref to build from. |
| `python-version` | | `"3.13"` | Build interpreter. Pure-Python wheels are `py3-none-any` regardless. |
| `dry-run` | | `false` | Build the wheel, skip tag/release/upload. |

## APWorld resolution

**Release tag prefix `<apworld>-<world_version>`**: The action requires the release event and
   parses the apworld from the tag. Tag your release as e.g. `generic-1.2.3`
   to publish `worlds/generic/` at `1.2.3`. This is the recommended path
   for repos that ship multiple worlds out of one repo (such as
   [TheLX5/Archipelago](https://github.com/TheLX5/Archipelago/)).

The Oliver-Multiworld-Squirrel GitHub App must also be installed on your repo
(read-only) so it can see the `workflow_run.completed` event and open the
Index PR on your behalf. Install it from
<https://github.com/apps/oliver-multiworld-squirrel>.

## Repo layout requirement

Your world's source must live at `worlds/<apworld>/` in your repo.
`archipelago.json` is required at `worlds/<apworld>/archipelago.json` and must
include `world_version`.

If you ship `worlds/<apworld>/pyproject.toml`, it is used as-is — `version`,
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
re-create the release to do so.

The action uploads without `--clobber`: re-running the workflow on a release
that already has a `.whl` asset will fail (`gh release upload` refuses to
overwrite). This is deliberate — the asset bytes are pinned by a
`#sha256=<hex>` fragment on the consumer side once Oliver opens the Index PR,
and a silent overwrite would invalidate that pin without warning. To fix a
transient build failure on an existing release, either
`gh release delete-asset <release_tag> <asset>` first, or delete and recreate
the entire release. Both are explicit human actions.

To publish a new build, **bump `world_version` in `archipelago.json`**, tag a
new release, and let the workflow run.

## Pinning the action

Pin to a major-version tag (`@v3`); patch updates fast-forward `v3`. Breaking
changes cut a new major. Pin to a SHA (`@<full-sha>`) for full reproducibility.

## Layout in this action repo

```
.github/workflows/
  build.yml                      per-world reusable workflow (workflow_call) -- worlds/<apworld>/ layout
  build-wheel.yml                pure-Python reusable workflow (workflow_call) -- flat-layout setuptools repos
scripts/
  shape_orphan.py                build the temp orphan tree from caller's worlds/<apworld>/
templates/
  pyproject.toml.j2              fallback per-world pyproject (only used when caller has none)
  README.md.j2                   auto-generated README that ships inside the wheel
```

## License

MIT (see `LICENSE`).
