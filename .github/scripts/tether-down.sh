#!/usr/bin/env bash
#
# Retire a preview's tether bookmark: discard everything its preview wrote,
# whether the PR merged or not. Used by preview-down.yml (one PR) and
# sweep.yml (every closed PR that still has a bookmark).
#
# Nothing a preview writes reaches prod; prod's own runs recompute with the
# merged code. Landing a fork instead is unsound three ways: only some
# backends can fast-forward (Icechunk and Iceberg; not Lance, Delta or Neon),
# so a merge would land some objects and drop others; the fork is kept across
# pushes, so it holds output of revisions that never merged; and with several
# previews open, only the first to merge could fast-forward at all. So every
# fork goes, merged or not.
#
# Usage: tether-down.sh <bookmark>
#
# Order matters and each step says why:
#   1. Fetch every pr* bookmark branch. tether reads bookmarks from the local
#      git branches, and `gc --prune-bookmarks` deletes the store branches of
#      every bookmark it cannot see -- so a checkout that knows only about this
#      PR would prune every open preview. Fetching them all first makes gc
#      precise.
#   2. Delete the preview's MLflow artifact prefix, DATA_ROOT_URI/mlflow/
#      <bookmark>/. MLflow's runs are service state on the forked db/mlflow,
#      and the artifacts those runs wrote go with them. (Artifacts are not a
#      tether object: tether's object-store backend has no fork, and the stack
#      derives the prefix per preview -- infra/preview/main.tf.)
#   3. Drop the local bookmark and let gc release its branches. --force-prune
#      is the only way unpinned writes go, which is the intent. The Neon fork
#      goes through Neon's API instead (drop_neon_fork below): tether 0.1.0b4's
#      gc cannot delete a branch that is its own storage. Deleting a Lance
#      branch needs the role's working-branch delete (aws/data-access
#      working_branch_prefix).
#   4. Delete stores the PR itself created (objects in its manifests that main
#      did not have where the PR branched off), under DATA_ROOT_URI only. Such
#      an object has nothing to fork from, so the preview wrote it at its real
#      location. If the PR merged, main's manifests now name it and prod's
#      first run creates it afresh. This is the outside-in version of a tether
#      feature in progress; once tether records store creation in its op log,
#      `gc` does this itself. It takes a delete on real store locations, which
#      only the default-branch teardown role holds (sweep.yml); Preview Down's
#      role cannot, and leaves them to the next sweep.
#   5. Delete the bookmark from origin, last: while anything above is still
#      owed -- a gc failure, artifacts or a store this role could not delete --
#      the bookmark stays, so the nightly sweep retries it.
#
# All git and tether commands act on the repository holding the dataset (see
# tether-env.sh): this one for packages/dataset, the dataset repo when
# DATASET_ROOT is a submodule. Both need its full history (gc keeps the pins
# any manifest in it references), so callers check out with fetch-depth 0.
#
# Env: DATA_ROOT_URI (s3://bucket/prefix/ under which the MLflow artifacts and
#      PR-created stores may be deleted; unset skips steps 2 and 4),
#      NEON_API_KEY, AWS credentials.

set -euo pipefail
# shellcheck source=tether-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/tether-env.sh"

bookmark="${1:?usage: tether-down.sh <bookmark>}"

# tether 0.1.0b4's gc plans a BRANCH_IS_STORAGE branch (Neon) without reading
# its head, then refuses the head it can read when it acts ("the plan could
# not read ... but it reads now"), so it can never delete the Neon fork, and
# fails the whole gc trying. This deletes the fork (and any `.<n>` sibling a
# reset left) in every Neon project the manifests name. A branch that still
# has branches hanging off it (pins someone took on the preview's branch,
# which gc releases) is left for the call after gc (`drop_neon_fork
# after-gc`), which warns if one still does.
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
head=$(dgit rev-parse "refs/heads/$bookmark")
owed="" # deletes this role could not make; the bookmark stays until they are

# Step 2: the preview's MLflow artifacts.
if [ -n "${DATA_ROOT_URI:-}" ]; then
  artifacts="${DATA_ROOT_URI%/}/mlflow/$bookmark/"
  if aws s3 ls "$artifacts" >/dev/null 2>&1; then
    echo "tether-down: deleting MLflow artifacts $artifacts"
    aws s3 rm --recursive --quiet "$artifacts" || owed="$owed $artifacts"
  fi
fi

# Step 3: the forks. A failure here exits with the bookmark still on origin.
dgit branch -D "$bookmark" >/dev/null
drop_neon_fork
tether gc --prune-bookmarks --force-prune --no-dry-run
drop_neon_fork after-gc

# Step 5, reached from each exit below: retire the bookmark unless a delete is
# still owed.
retire() {
  if [ -n "$owed" ]; then
    echo "::warning::tether-down: could not delete$owed (this role cannot delete store locations); keeping $bookmark for the nightly sweep's teardown role."
    return
  fi
  dgit push --quiet origin --delete "$bookmark" || echo "tether-down: bookmark branch already gone from origin"
}
[ -n "${DATA_ROOT_URI:-}" ] || { retire; exit 0; }

# Step 4: stores this PR created. Where the PR branched off: the newest commit
# on main's first-parent line that the bookmark contains -- however the PR
# merged (merge commit, squash, rebase), or if it never did, its own commits
# are not on that line. Its manifests versus the bookmark's; kinds whose store
# is a prefix tether/the job created; URIs under DATA_ROOT_URI only. Paths are
# relative to the dataset root (git resolves pathspecs and `./` object paths
# against the cwd), so this reads the same in both layouts.
base=""
while IFS= read -r commit; do
  if dgit merge-base --is-ancestor "$commit" "$head"; then
    base=$commit
    break
  fi
done < <(dgit rev-list --first-parent refs/remotes/origin/main)
if [ -z "$base" ]; then
  echo "::warning::tether-down: $bookmark shares no history with main; leaving any stores it created."
  retire
  exit 0
fi
while IFS= read -r manifest; do
  [ -n "$manifest" ] || continue
  toml=$(dgit show "$head:./$manifest")
  kind=$(sed -n 's/^kind *= *"\([^"]*\)".*/\1/p' <<<"$toml")
  uri=$(sed -n 's/^uri *= *"\([^"]*\)".*/\1/p' <<<"$toml")
  case "$kind" in icechunk | lance | delta) ;; *) continue ;; esac
  case "$uri" in "$DATA_ROOT_URI"*) ;; *) echo "tether-down: leaving $uri (outside DATA_ROOT_URI)"; continue ;; esac
  echo "tether-down: deleting store $uri created by $bookmark"
  aws s3 rm --recursive --quiet "${uri%/}/" || owed="$owed $uri"
done < <(comm -13 \
  <(dgit ls-tree -r --name-only "$base" -- .tether/objects | sort) \
  <(dgit ls-tree -r --name-only "$head" -- .tether/objects | sort))
retire
