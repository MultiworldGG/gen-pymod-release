# MultiworldGG/build-and-publish-action

Reusable GitHub workflow for per-world MultiworldGG repos. On each release of a
world, this action:

1. Reads `worlds/<slug>/archipelago.json` from your repo
2. Builds a pip-installable orphan-branch tree (`pyproject.toml` + `src/worlds/<slug>/...`)
3. Force-pushes that tree to the `wheel/worlds/<slug>` branch of your repo
4. Tags it immutably as `wheel/worlds/<slug>/<world_version>`

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
  contents: write   # for the wheel branch + tag push to this repo

jobs:
  publish:
    uses: MultiworldGG/build-and-publish-action/.github/workflows/build.yml@v3
    # No `with:` — slug comes from vars.WORLD_FOLDER_NAME
    # No `secrets:` — no Oliver secrets needed
```

Then set one repository variable: Settings → Secrets and variables → Actions →
Variables → New: `WORLD_FOLDER_NAME=<slug>` (e.g. `WORLD_FOLDER_NAME=clique`).

The Oliver-Multiworld-Squirrel GitHub App must also be installed on your repo
(read-only) so it can see the `workflow_run.completed` event and open the
Index PR on your behalf. Install it from
<https://github.com/apps/oliver-multiworld-squirrel>.

## Repo layout requirement

Your world's source must live at `worlds/<slug>/` in your repo. `archipelago.json`
is required at `worlds/<slug>/archipelago.json` and must include `world_version`
(used as the immutable tag suffix).

If you ship `worlds/<slug>/pyproject.toml`, it is used as-is — `version`,
`authors`, and `description` are injected from `archipelago.json` only when
missing. Otherwise a minimal default is generated.

## Inputs

| name | required | default | notes |
|---|---|---|---|
| `source-ref` | | release tag, else `github.sha` | What to check out from your repo. |
| `dry-run` | | `false` | Shape + (optionally) build the wheel; skip push. |
| `pip-build-check` | | `true` | Run `python -m build` against the shaped tree before pushing. |
| `allow-tag-reuse` | | `false` | Allow re-publishing the same tag, but only if the new commit SHA matches the old. |

## Versioning constraint (important)

The tag `wheel/worlds/<slug>/<world_version>` is **immutable**. The action will
**fail hard** if you try to re-publish a `world_version` that already has a
tag at a different SHA. This is intentional: the Index manifest's
`module_location` may pin live deployments and saved generations to the old
tag — overwriting it would break them.

To publish a new build, **bump `world_version` in `archipelago.json`** and
re-run.

## Pinning the action

Pin to a major-version tag (`@v3`); patch updates fast-forward `v3`. Breaking
changes cut a new major. Pin to a SHA (`@<full-sha>`) for full reproducibility.

## How the orphan branch is laid out

```
pyproject.toml
src/
  worlds/
    <slug>/
      ...your world's source, copied wholesale from worlds/<slug>/...
README.md
```

This is a [PEP 420 namespace package](https://peps.python.org/pep-0420/)
contribution to the shared `worlds.` namespace. It coexists with all other
per-world install branches and with the MultiworldGG monorepo's namespace stub.

## Layout in this action repo

```
.github/workflows/build.yml      reusable workflow (workflow_call)
scripts/
  shape_orphan.py                build the temp orphan tree from caller's worlds/<slug>/
  push_wheel_branch.sh           orphan branch + immutable tag push
templates/
  pyproject.toml.j2              fallback per-world pyproject (only used when caller has none)
  README.md.j2                   auto-generated README for the orphan branch
```

## License

MIT (see `LICENSE`).
