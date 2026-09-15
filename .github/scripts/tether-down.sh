#!/usr/bin/env bash
#
# Retire a preview's tether bookmark: land its data if the PR merged, then
# release its branches in every store. Used by preview-down.yml (one PR) and
# sweep.yml (every closed PR that still has a bookmark).
#
# Usage: tether-down.sh <bookmark> <merged: true|false>
#
# Order matters and each step says why:
#   1. Fetch every pr* bookmark branch. tether reads bookmarks from the local
#      git branches, and `gc --prune-bookmarks` deletes the store branches of
#      every bookmark it cannot see -- so a checkout that knows only about this
#      PR would prune every open preview. Fetching them all first makes gc
#      precise.
#   2. Merged: join the bookmark, pin its branch heads (`commit`), and
#      fast-forward the promotable stores (`promote KEY...`). Neon cannot
#      promote (the schema reaches prod through deploy.yml's alembic step; the
#      rows were test data), so promotion is by key. A refused fast-forward
#      means prod moved since the fork: the honest outcome is a recompute by
#      prod's Dagster with the merged code, and the branches are KEPT so nothing
#      is lost until someone decides.
#   3. Drop the bookmark and let gc judge its branches. --force-prune is the
#      only way BRANCH_IS_STORAGE systems (Neon) and unpinned writes go, which
#      for an abandoned preview is the intent.
#   4. Not merged: delete stores the PR itself created (objects in its
#      manifests that main's do not have), under DATA_ROOT_URI only. This is the
#      outside-in version of a tether feature in progress; once tether records
#      store creation in its op log, `gc` does this itself.
#
# All git and tether commands act on the repository holding the dataset (see
# tether-env.sh): this one for packages/dataset, the dataset repo when
# DATASET_ROOT is a submodule.
#
# Env: PROMOTABLE_KEYS (space-separated keys `promote` may fast-forward),
#      DATA_ROOT_URI (s3://bucket/prefix/ under which PR-created stores may be
#      deleted; unset skips step 4), NEON_API_KEY, AWS credentials.

set -euo pipefail
# shellcheck source=tether-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/tether-env.sh"

bookmark="${1:?usage: tether-down.sh <bookmark> <merged>}"
merged="${2:?usage: tether-down.sh <bookmark> <merged>}"
promotable="${PROMOTABLE_KEYS:-}"

dgit fetch --quiet origin '+refs/heads/pr*:refs/heads/pr*' '+refs/heads/main:refs/remotes/origin/main' || true

if ! dgit show-ref --verify --quiet "refs/heads/$bookmark"; then
  echo "tether-down: no bookmark $bookmark on origin; nothing forked (or already retired)."
  exit 0
fi

landed=true
if [ "$merged" = "true" ]; then
  landed=false
  current=$(dgit rev-parse --abbrev-ref HEAD)
  dgit checkout --quiet "$bookmark"
  if tether new "$bookmark" &&
    tether commit -m "data written by $bookmark" --force &&
    # shellcheck disable=SC2086
    tether promote $promotable --strategy ff; then
    landed=true
    echo "tether-down: landed $bookmark on main for: $promotable"
    echo "::notice::$bookmark's data landed on prod ($promotable). Manifests on main catch up with the nightly data-pull."
  else
    echo "::warning::$bookmark merged but its data could not be fast-forwarded (prod moved, or a store refused). Branches KEPT. Either let prod recompute with the merged code and retire the bookmark by hand -- tether abandon / jj bookmark delete $bookmark / tether gc --prune-bookmarks --force-prune -- or land it from a checkout: tether new $bookmark && tether promote <keys>."
  fi
  dgit checkout --quiet "$current"
fi

if [ "$landed" != "true" ]; then
  exit 0
fi

dgit branch -D "$bookmark" >/dev/null
tether gc --prune-bookmarks --force-prune --no-dry-run
dgit push --quiet origin --delete "$bookmark" || echo "tether-down: bookmark branch already gone from origin"

if [ "$merged" = "true" ] || [ -z "${DATA_ROOT_URI:-}" ]; then
  exit 0
fi

# Step 4: stores this PR created. Its manifests versus main's, kinds whose
# store is a prefix tether/the job created, URIs under DATA_ROOT_URI only.
# Paths are relative to the dataset root (git resolves pathspecs and `./`
# object paths against the cwd), so this reads the same in both layouts.
comm -13 \
  <(dgit ls-tree -r --name-only origin/main -- .tether/objects | sort) \
  <(dgit ls-tree -r --name-only "origin/$bookmark" -- .tether/objects 2>/dev/null | sort) |
  while IFS= read -r manifest; do
    [ -n "$manifest" ] || continue
    toml=$(dgit show "origin/$bookmark:./$manifest" 2>/dev/null || true)
    kind=$(sed -n 's/^kind *= *"\([^"]*\)".*/\1/p' <<<"$toml")
    uri=$(sed -n 's/^uri *= *"\([^"]*\)".*/\1/p' <<<"$toml")
    case "$kind" in icechunk | lance | delta) ;; *) continue ;; esac
    case "$uri" in "$DATA_ROOT_URI"*) ;; *) echo "tether-down: leaving $uri (outside DATA_ROOT_URI)"; continue ;; esac
    echo "tether-down: deleting store $uri created by $bookmark"
    aws s3 rm --recursive --quiet "${uri%/}/"
  done
