#!/usr/bin/env bash
#
# Fork every registered store for one preview and hand the addresses to the
# stack. Runs in preview-up.yml's fork-data job (tether mode only).
#
# Usage: tether-fork.sh <bookmark>          # e.g. pr123
#
# A PR's first run: `tether new -b <bookmark> --eager` creates the bookmark at
# the checked-out commit of the dataset and, because tether.toml says eager,
# one branch per store right now (tether.ws.<dataset>.<bookmark>), forked from
# the last pinned state on main. `--discard` covers store branches a retired
# preview of the same PR left behind. The bookmark is pushed as a git branch
# of the repository that holds the dataset (this one, or the dataset repo when
# DATASET_ROOT is a submodule) so preview-down.yml and sweep.yml can see which
# previews still hold forks from any checkout -- see tether-down.sh for why
# that matters to `gc`.
#
# Later pushes keep the fork as it is, as tofu mode keeps its Neon branch: data
# and service state carry over from one revision of the PR to the next.
# `tether new <bookmark> --adopt` takes the bookmark's store branches as they
# are, writes included, and binds only to each one still existing, not to its
# head, which a running preview's services (Dagster's daemon above all) move
# every few seconds. It runs in a worktree of the bookmark, because joining a
# bookmark checks out its commit -- the PR's head at its first run -- which in
# this checkout would swap these scripts out from under the run. A store the
# PR registers after its first run is not on that commit, so it is forked when
# the PR is re-labelled (remove and re-add `preview`), which starts over from a
# fresh fork of the baseline.
#
# Output: tfvars_b64 in $GITHUB_OUTPUT -- base64 of the JSON object
# tether-open-all.sh wrote, for the reusable workflow's extra_tfvars_json.
#
# Env: NEON_API_KEY and AWS credentials.

set -euo pipefail
# shellcheck source=tether-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/tether-env.sh"

bookmark="${1:?usage: tether-fork.sh <bookmark>}"
out="${RUNNER_TEMP:-/tmp}/tether-tfvars.json"

if dgit ls-remote --exit-code --heads origin "$bookmark" >/dev/null; then
  dgit fetch --quiet origin "+refs/heads/$bookmark:refs/heads/$bookmark"
  wt="${RUNNER_TEMP:-/tmp}/tether-$bookmark"
  prefix=$(dgit rev-parse --show-prefix)
  dgit worktree add --quiet "$wt" "$bookmark"
  export DATASET_ROOT="$wt/$prefix"
  tether new "$bookmark" --adopt
  .github/scripts/tether-open-all.sh "$out"
  verb="kept"
else
  tether new -b "$bookmark" --eager --discard
  dgit push --force --quiet origin "HEAD:refs/heads/$bookmark"
  .github/scripts/tether-open-all.sh "$out"
  verb="forked"
fi

jq -e 'type == "object" and (.fork_dbs | has("app")) and has("data_refs")' "$out" >/dev/null || {
  echo "tether-fork: no usable object in $out" >&2
  exit 1
}
echo "$verb $(jq -r '.data_refs | fromjson | keys | join(", ")' "$out") plus $(jq -r '.fork_dbs | keys | map("db/" + .) | join(", ")' "$out") on $bookmark"
echo "tfvars_b64=$(base64 < "$out" | tr -d '\n')" >> "${GITHUB_OUTPUT:-/dev/stdout}"
