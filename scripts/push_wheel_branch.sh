#!/usr/bin/env bash
# Push the shaped orphan tree as an orphan commit to:
#   - branch:  wheel/worlds/<slug>                              (force-push, "latest" pointer)
#   - tag:     wheel/worlds/<slug>/<world_version>              (NEVER overwritten — see check below)
#
# Hard requirement (versioning constraint from the design):
#   Reusing a world_version that already has an immutable tag must NOT silently
#   overwrite the tag. Live deployments and saved generations may be pinned to
#   the existing tag's SHA via the Index manifest's module_location; clobbering
#   it would break them. The check below errors out when the tag exists at a
#   different SHA, with a maintainer-actionable message.
#
# Inputs (env vars):
#   ORPHAN_TREE       absolute path to the shaped tree (output of shape_orphan.py)
#   WORLD_VERSION     e.g. "7.0.2" — used as tag suffix
#   WORLD_SLUG        e.g. "oot" — used as branch + tag prefix and in log messages
#   CALLER_REPO       e.g. "MultiworldGG/MultiworldGG" — caller's owner/repo
#   GH_TOKEN          token with `contents: write` on CALLER_REPO
#   COMMIT_MESSAGE    message for the orphan commit
#   GIT_USER_NAME     committer name (e.g. "github-actions[bot]")
#   GIT_USER_EMAIL    committer email
#   ALLOW_TAG_REUSE   if "1", and the tag exists, only fail when the existing
#                     tag points to a DIFFERENT SHA from the about-to-push tree.
#                     Default 0 (any preexisting tag is a hard error).

set -euo pipefail

require() {
    if [ -z "${!1:-}" ]; then
        echo "::error::Missing required env var: $1"
        exit 2
    fi
}

require ORPHAN_TREE
require WORLD_VERSION
require WORLD_SLUG
require CALLER_REPO
require GH_TOKEN
require COMMIT_MESSAGE
require GIT_USER_NAME
require GIT_USER_EMAIL

ALLOW_TAG_REUSE="${ALLOW_TAG_REUSE:-0}"

if [ ! -d "${ORPHAN_TREE}" ]; then
    echo "::error::ORPHAN_TREE does not exist: ${ORPHAN_TREE}"
    exit 2
fi

TAG="wheel/worlds/${WORLD_SLUG}/${WORLD_VERSION}"
BRANCH="wheel/worlds/${WORLD_SLUG}"
REMOTE_URL="https://x-access-token:${GH_TOKEN}@github.com/${CALLER_REPO}.git"
SAFE_REMOTE_URL="https://x-access-token:***@github.com/${CALLER_REPO}.git"

# Build the orphan commit in a fresh dir — never touches the caller's working tree.
WORK="$(mktemp -d)/orphan-build"
mkdir -p "${WORK}"
cp -r "${ORPHAN_TREE}/." "${WORK}/"

cd "${WORK}"
git init -q --initial-branch="${BRANCH}"
git config user.name "${GIT_USER_NAME}"
git config user.email "${GIT_USER_EMAIL}"
git add .
git commit -q -m "${COMMIT_MESSAGE}"
NEW_SHA="$(git rev-parse HEAD)"
echo "Orphan commit built: ${NEW_SHA}"

git remote add caller-origin "${REMOTE_URL}"

# ---------- TAG IMMUTABILITY CHECK ----------
EXISTING_TAG_LINE="$(git ls-remote --tags caller-origin "refs/tags/${TAG}" 2>/dev/null || true)"
SKIP_TAG_PUSH=""
if [ -n "${EXISTING_TAG_LINE}" ]; then
    EXISTING_TAG_SHA="$(printf '%s' "${EXISTING_TAG_LINE}" | awk '{print $1}')"
    echo "Existing tag ${TAG} found at ${EXISTING_TAG_SHA}"
    if [ "${ALLOW_TAG_REUSE}" = "1" ] && [ "${EXISTING_TAG_SHA}" = "${NEW_SHA}" ]; then
        echo "ALLOW_TAG_REUSE=1 and tag SHA matches new commit — no-op republish allowed."
        echo "Skipping tag push (already exists, identical) but still updating the branch tip."
        SKIP_TAG_PUSH=1
    else
        cat <<EOF
::error::Tag ${TAG} already exists on ${CALLER_REPO} at ${EXISTING_TAG_SHA}.
World ${WORLD_SLUG} is trying to publish version ${WORLD_VERSION} again, but
that version already has an immutable tag pointing to a different commit.

Reusing a world_version is not allowed: live deployments and saved generations
may be pinned to the existing tag's SHA via the Index manifest's module_location.
Overwriting it would break them.

Fix: bump world_version in worlds/${WORLD_SLUG}/archipelago.json and re-run.
(If you really do need to re-publish identical content under this tag, set
ALLOW_TAG_REUSE=1 — it's allowed only when the new commit SHA equals the old.)
EOF
        exit 1
    fi
fi

# ---------- PUSH ----------
PUSH_REFS=("HEAD:refs/heads/${BRANCH}")
if [ -z "${SKIP_TAG_PUSH}" ]; then
    PUSH_REFS+=("HEAD:refs/tags/${TAG}")
fi
echo "Pushing to ${SAFE_REMOTE_URL}: ${PUSH_REFS[*]}"
git push --force caller-origin "${PUSH_REFS[@]}"
echo "Pushed branch ${BRANCH} and tag ${TAG}."

# Emit step outputs for downstream steps.
if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
        echo "tag=${TAG}"
        echo "branch=${BRANCH}"
        echo "sha=${NEW_SHA}"
    } >> "${GITHUB_OUTPUT}"
fi
