# MultiworldGG/build-and-publish-action

Reusable GitHub workflow for per-world MultiworldGG repos. On each release of a
world, this action:

1. Reads `worlds/<slug>/archipelago.json` from your repo
2. Builds a pip-installable orphan-branch tree (`pyproject.toml` + `src/worlds/<slug>/...`)
3. Force-pushes that tree to the `module-install` branch of your repo
4. Tags it immutably as `module-install/<world_version>`
5. Opens a PR against [MultiworldGG-Index](https://github.com/lallaria/MultiworldGG-Index)
   updating your world's `module_location` to the new tag URL

The Index PR triggers Greg's 7-check security review automatically.

## Quick start (caller workflow)

In your per-world repo, create `.github/workflows/publish-to-index.yml`:

```yaml
name: Publish to MultiworldGG-Index
on:
  release:
    types: [published]
  workflow_dispatch: {}

jobs:
  publish:
    uses: MultiworldGG/build-and-publish-action/.github/workflows/build.yml@v1
    with:
      slug: oot          # ← your world's slug (filename stem, lowercase, snake_case)
    secrets:
      INDEX_PR_TOKEN: ${{ secrets.MWGG_INDEX_PR_TOKEN }}
```

Then add a single repo Secret:

- **`MWGG_INDEX_PR_TOKEN`** — fine-grained PAT (or GitHub App installation token)
  with **Contents: Write** + **Pull requests: Write** on
  [`lallaria/MultiworldGG-Index`](https://github.com/lallaria/MultiworldGG-Index).

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
| `slug` | ✓ | — | Filename stem of `worlds/<slug>/`. Lowercase + underscores. |
| `source-ref` | | release tag, else `github.sha` | What to check out from your repo. |
| `index-repo` | | `lallaria/MultiworldGG-Index` | Override for testing/forks. |
| `dry-run` | | `false` | Shape + (optionally) build the wheel; skip push + Index PR. |
| `pip-build-check` | | `true` | Run `python -m build` against the shaped tree before pushing. |
| `allow-tag-reuse` | | `false` | Allow re-publishing the same tag, but only if the new commit SHA matches the old. |

## Versioning constraint (important)

The tag `module-install/<world_version>` is **immutable**. The action will
**fail hard** if you try to re-publish a `world_version` that already has a
tag at a different SHA. This is intentional: the Index manifest's
`module_location` may pin live deployments and saved generations to the old
tag — overwriting it would break them.

To publish a new build, **bump `world_version` in `archipelago.json`** and
re-run.

## Pinning the action

Pin to a major-version tag (`@v1`); patch updates fast-forward `v1`. Breaking
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
  push_module_install.sh         orphan branch + immutable tag push
  open_index_pr.py               clone Index, edit worlds/<slug>.json, push branch, gh pr create
templates/
  pyproject.toml.j2              fallback per-world pyproject (only used when caller has none)
  README.md.j2                   auto-generated README for the orphan branch
```

## License

MIT (see `LICENSE`).
