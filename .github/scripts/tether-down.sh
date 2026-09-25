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
#   3. Always -- merged or not, landed or not: delete the preview's MLflow
#      artifact prefix, DATA_ROOT_URI/mlflow/<bookmark>/. MLflow's runs are
#      service state on the forked db/mlflow, which Neon cannot promote, so
#      the artifacts those runs wrote are test output in every outcome; a
#      failed landing in step 2 keeps branches that might still land, never
#      these. (Artifacts are not a tether object: tether's object-store
#      backend has no fork, and the stack derives the prefix per preview --
#      infra/preview/main.tf.)
#   4. Drop the bookmark and let gc judge its branches (skipped when step 2
#      kept them). --force-prune is the only way unpinned writes go, which for
#      an abandoned preview is the intent. The Neon fork goes through Neon's
#      API instead (drop_neon_fork below): tether 0.1.0b4's gc cannot delete a
#      branch that is its own storage.
#   5. Not merged: delete stores the PR itself created (objects in its
#      manifests that main's do not have), under DATA_ROOT_URI only. This is the
#      outside-in version of a tether feature in progress; once tether records
#      store creation in its op log, `gc` does this itself.
#
# All git and tether commands act on the repository holding the dataset (see
# tether-env.sh): this one for packages/dataset, the dataset repo when
# DATASET_ROOT is a submodule.
#
# Env: PROMOTABLE_KEYS (space-separated keys `promote` may fast-forward),
#      DATA_ROOT_URI (s3://bucket/prefix/ under which the MLflow artifacts and
#      PR-created stores may be deleted; unset skips steps 3 and 5),
#      NEON_API_KEY, AWS credentials.

set -euo pipefail
# shellcheck source=tether-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/tether-env.sh"

bookmark="${1:?usage: tether-down.sh <bookmark> <merged>}"
merged="${2:?usage: tether-down.sh <bookmark> <merged>}"
promotable="${PROMOTABLE_KEYS:-}"

# tether 0.1.0b4's gc plans a BRANCH_IS_STORAGE branch (Neon) without reading
# its head, then refuses the head it can read when it acts ("the plan could
# not read ... but it reads now"), so it can never delete the Neon fork, and
# fails the whole gc trying. This deletes the fork (and any `.<n>` sibling a
# reset left) in every Neon project the manifests name. A branch that still
# has branches hanging off it -- the pins a merge's `tether commit` took,
# which gc releases -- is left for the call after gc (`drop_neon_fork after-gc`),
# which warns if one still does.
neon_api() {
  curl -fsS -H "Authorization: Bearer $NEON_API_KEY" -H "Accept: application/json" "$@"
}
drop_neon_fork() {
  local objects dataset fork projects project branches id tries
  objects="$DATASET_ROOT/.tether/objects"
  dataset=$(sed -n 's/^id *= *"\([^"]*\)".*/\1/p' "$DATASET_ROOT/tether.toml" | head -1)
  fork="tether.ws.$dataset.$bookmark"
  projects=$(grep -rl '^kind *= *"neon"' "$objects" | while IFS= read -r manifest; do
    sed -n 's/^project_id *= *"\([^"]*\)".*/\1/p' "$manifest"
  done | sort -u)
  for project in $projects; do
    branches=$(neon_api "https://console.neon.tech/api/v2/projects/$project/branches")
    for id in $(jq -r --arg f "$fork" '.branches[] | select(.name | test("^" + ($f | gsub("[.]"; "\\.")) + "([.][0-9]+)?$")) | .id' <<<"$branches"); do
      if jq -e --arg id "$id" 'any(.branches[]; .parent_id == $id)' <<<"$branches" >/dev/null; then
        if [ "${1:-}" = after-gc ]; then
          echo "::warning::Neon branch $id ($fork) in project $project still has branches hanging off it after gc; delete them, then it, by hand."
        else
          echo "tether-down: Neon branch $id ($fork) has branches hanging off it (pins gc releases); deleting it after gc"
        fi
        continue
      fi
      if jq -e --arg id "$id" 'any(.branches[]; .id == $id and .protected == true)' <<<"$branches" >/dev/null; then
        neon_api -X PATCH -H "Content-Type: application/json" -d '{"branch":{"protected":false}}' \
          "https://console.neon.tech/api/v2/projects/$project/branches/$id" >/dev/null
      fi
      echo "tether-down: deleting Neon branch $id ($fork) in project $project"
      neon_api -X DELETE "https://console.neon.tech/api/v2/projects/$project/branches/$id" >/dev/null
      # gc lists branches next; wait until Neon stops listing this one.
      for tries in $(seq 1 30); do
        branches=$(neon_api "https://console.neon.tech/api/v2/projects/$project/branches")
        jq -e --arg id "$id" 'any(.branches[]; .id == $id)' <<<"$branches" >/dev/null || break
        [ "$tries" -lt 30 ] || { echo "tether-down: Neon still lists branch $id" >&2; return 1; }
        sleep 2
      done
    done
  done
}

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
  # shellcheck disable=SC2086 # $promotable is a list of keys
  if tether new "$bookmark" &&
    tether commit -m "data written by $bookmark" --force &&
    tether promote $promotable --strategy ff; then
    landed=true
    echo "tether-down: landed $bookmark on main for: $promotable"
    echo "::notice::$bookmark's data landed on prod ($promotable). Manifests on main catch up with the nightly data-pull."
  else
    echo "::warning::$bookmark merged but its data could not be fast-forwarded (prod moved, or a store refused). Branches KEPT. Either let prod recompute with the merged code and retire the bookmark by hand -- tether abandon / jj bookmark delete $bookmark / tether gc --prune-bookmarks --force-prune -- or land it from a checkout: tether new $bookmark && tether promote <keys>."
  fi
  dgit checkout --quiet "$current"
fi

# Step 3: the preview's MLflow artifacts, before any exit (see the header).
if [ -n "${DATA_ROOT_URI:-}" ]; then
  artifacts="${DATA_ROOT_URI%/}/mlflow/$bookmark/"
  if aws s3 ls "$artifacts" >/dev/null 2>&1; then
    echo "tether-down: deleting MLflow artifacts $artifacts"
    aws s3 rm --recursive --quiet "$artifacts"
  fi
fi

if [ "$landed" != "true" ]; then
  exit 0
fi

dgit branch -D "$bookmark" >/dev/null
drop_neon_fork
tether gc --prune-bookmarks --force-prune --no-dry-run
drop_neon_fork after-gc
dgit push --quiet origin --delete "$bookmark" || echo "tether-down: bookmark branch already gone from origin"

if [ "$merged" = "true" ] || [ -z "${DATA_ROOT_URI:-}" ]; then
  exit 0
fi

# Step 5: stores this PR created. Its manifests versus main's, kinds whose
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
