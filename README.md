# MultiworldGG/gen-pymod-release

> **Looking for a step-by-step author guide?** See the [MultiworldGG-Index docs site](https://multiworldgg.github.io/MultiworldGG-Index/) — "I want the easiest setup" or "I want to write my own `pyproject.toml`".
>
> This README is the workflow reference (inputs, outputs, version-pinning rules).

Reusable GitHub workflow for per-world MultiworldGG repos. When you want to release a world
  this action:

1. Reads `worlds/<apworld>/archipelago.json` from your repo at the release tag's
   checked-out source
2. Builds a pip-installable wheel (`<dist>-<world_version>-py3-none-any.whl`)
3. Uploads the wheel as an asset on the GitHub release

The matching `MultiworldGG-Index` PR is opened separately by the
**Oliver-Multiworld-Squirrel** GitHub App after the completed release is
published. Karen's review checks fire automatically once that PR is open. None
of that lives in this action.

## Quick start — per-world repo

In your per-world repo, create `.github/workflows/make_pyproject.yml`. The
workflow can still respond to published releases, but the recommended path is
to let the helper dispatch it against an existing draft before publication:

```yaml
name: Create and Release Python Package
on:
  release:
    types: [published]
  workflow_dispatch:
    inputs:
      release-tag:
        description: "Existing draft release tag, e.g. myclgm-1.2.3"
        required: true
        type: string
      source-ref:
        description: "Git ref to build. Defaults to release-tag."
        required: false
        type: string
      apworld:
        description: "World folder name under worlds/<apworld>/, e.g. myclgm"
        required: false
        type: string

permissions:
  contents: write   # for `gh release upload`

jobs:
  publish:
    uses: MultiworldGG/gen-pymod-release/.github/workflows/build.yml@v3
    with:
      release-tag: ${{ inputs['release-tag'] || github.event.release.tag_name }}
      source-ref: ${{ inputs['source-ref'] || inputs['release-tag'] || github.event.release.tag_name }}
      apworld: ${{ inputs.apworld }}
```

Then run the draft-release helper from your world repo:

```bash
python /path/to/gen-pymod-release/scripts/prepare_apworld_release.py \
  --apworld myclgm \
  --version 1.2.3 \
  --notes RELEASE_NOTES.md \
  --open
```

The helper reads `worlds/<apworld>/archipelago.json`, persists `repo_url`,
normalizes `world_version`, commits that manifest metadata, pushes the branch
and `<apworld>-<world_version>` tag, creates or reuses a draft GitHub Release,
dispatches `make_pyproject.yml`, waits for it, and verifies that the draft has
exactly one wheel asset. Review the attached assets in GitHub and click
**Publish**. Wheel uploads intentionally do not use `--clobber`.

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

**Release tag prefix `<apworld>-<world_version>`**: On release events, the
action parses the apworld from the tag. Tag your release as e.g.
`generic-1.2.3` to publish `worlds/generic/` at `1.2.3`.

**Draft releases:** Use `scripts/prepare_apworld_release.py` to create the tag
and draft release, then dispatch the caller workflow with `release-tag` so the
wheel and optional `.apworld` are attached before publication.

**Manual dry-runs:** If your caller workflow exposes `workflow_dispatch`, pass
the `apworld` input and set `dry-run: true`. If you pass `release-tag`, uploads
target that release; without `release-tag`, the workflow builds only and skips
release upload.

Oliver-the-Multiworld-Squirrel GitHub App must also be installed on your repo
(read-only) so it can see the published release and open the Index PR on your
behalf. Install it from
<https://github.com/apps/oliver-the-multiworld-squirrel>.

## Building a `.apworld` alongside the wheel

A `.apworld` is the zipped-folder format end-users drop into `custom_worlds/`.
It is built by `Launcher.py "Build APWorlds"` and is separate from the
pip-installable wheel that Oliver consumes for the Index PR.

Add a second job to your `make_pyproject.yml` to produce both artifacts from
a single draft release:

```yaml
name: Create and Release Python Package
on:
  release:
    types: [published]
  workflow_dispatch:
    inputs:
      release-tag:
        description: "Existing draft release tag, e.g. myclgm-1.2.3"
        required: true
        type: string
      source-ref:
        description: "Git ref to build. Defaults to release-tag."
        required: false
        type: string
      apworld:
        description: "World folder name under worlds/<apworld>/, e.g. myclgm"
        required: false
        type: string

permissions:
  contents: write

jobs:
  publish-wheel:
    uses: MultiworldGG/gen-pymod-release/.github/workflows/build.yml@v3
    with:
      release-tag: ${{ inputs['release-tag'] || github.event.release.tag_name }}
      source-ref: ${{ inputs['source-ref'] || inputs['release-tag'] || github.event.release.tag_name }}
      apworld: ${{ inputs.apworld }}
  publish-apworld:
    uses: MultiworldGG/gen-pymod-release/.github/workflows/build-apworld.yml@v3
    with:
      game: "Your Game Name"
      release-tag: ${{ inputs['release-tag'] || github.event.release.tag_name }}
      source-ref: ${{ inputs['source-ref'] || inputs['release-tag'] || github.event.release.tag_name }}
      apworld: ${{ inputs.apworld }}
```

`publish-wheel` is what Oliver consumes (the wheel URL gets a `#sha256=` pin in
the Index PR). `publish-apworld` is for users who install `.apworld` files
directly into `custom_worlds/`. Drop either job if you do not need it.

### Inputs for `build-apworld.yml`

| name | required | default | notes |
|---|---|---|---|
| `game` | **yes** | — | The world's display name — the value of `World.game` in your Python class (e.g. `"My Cool Game"`). Must match exactly; the Launcher looks worlds up by display name. |
| `mwgg-ref` | | `"main"` | Ref of `MultiworldGG/MultiworldGG` to check out as the Launcher host. Pin to a release tag (e.g. `"0.6.1"`) for reproducibility. Always resolves against the canonical monorepo, not your fork. Ignored when `from-fork: true`. |
| `from-fork` | | `false` | Set to `true` when calling from an Archipelago fork (a full source tree with its own `Launcher.py`). Skips the canonical MWGG checkout and builds from the caller's own tree; `mwgg-ref` is ignored. |
| `release-tag` | | `""` | Existing GitHub Release tag to upload to. Required for pre-publication draft uploads from caller `workflow_dispatch`; release events default from `github.event.release.tag_name`. |
| `source-ref` | | release tag, else `github.sha` | Caller-repo ref to check out for the world source. Prefer this input for new callers. |
| `apworld` | | `""` | World folder name under `worlds/<apworld>/`. Ignored on release events, where the apworld is parsed from the release tag prefix. Required for manual/non-release dry-runs. |
| `apworld-source-ref` | | release tag, else `github.sha` | Legacy alias for `source-ref`; kept for existing callers. |
| `dry-run` | | `false` | Build the `.apworld` but skip `gh release upload`. |

### Output

A single `<apworld>.apworld` file attached to the GitHub release as an asset.
The apworld is inferred from the release tag prefix (`<apworld>-<version>`).
The upload uses `--clobber` — unlike the wheel, the `.apworld` is not pinned
by a SHA256 fragment on the Index side, so overwriting on workflow re-runs is
safe.

```
https://github.com/<owner>/<repo>/releases/download/<release_tag>/<apworld>.apworld
```

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
| `release-tag` | | `""` | Existing GitHub Release tag to upload to. Required for pre-publication draft uploads from caller `workflow_dispatch`; release events default from `github.event.release.tag_name`. |
| `apworld` | | `""` | World folder name under `worlds/<apworld>/`. Ignored on release events, where the apworld is parsed from the release tag prefix. Required for manual/non-release dry-runs. |
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
  build-apworld.yml              per-world reusable workflow (workflow_call) -- builds .apworld via Launcher.py
  build-wheel.yml                pure-Python reusable workflow (workflow_call) -- flat-layout setuptools repos
scripts/
  prepare_apworld_release.py       local helper that creates the tag + draft GitHub Release
  shape_tree.py                  build the temp orphan tree from caller's worlds/<apworld>/
templates/
  pyproject.toml.j2              fallback per-world pyproject (only used when caller has none)
  README.md.j2                   auto-generated README that ships inside the wheel
```

## License

MIT (see `LICENSE`).
