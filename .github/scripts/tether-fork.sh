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
# and service state carry over from one revision of the PR to the next. tether
# is not run again. In tether 0.1.0b4 every `tether new` on an existing
# bookmark, a reset or a join alike, needs each branch's head unchanged between
# its plan and its apply, and a running preview's services (Dagster's daemon
# above all) write its Neon branch every few seconds. Instead the addresses
# come back from the first run's artifact, tether-fork-<bookmark>, which
# preview-up.yml uploads on every run. A store the PR adds after its first
# run is forked when the PR is re-labelled (remove and re-add `preview`),
# which starts over from a fresh fork of the baseline.
#
# Output: tfvars_b64 in $GITHUB_OUTPUT -- base64 of the JSON object
# tether-open-all.sh wrote (kept at $RUNNER_TEMP/tether-tfvars.json for the
# upload), for the reusable workflow's extra_tfvars_json.
#
# Env: NEON_API_KEY and AWS credentials (first run); GH_TOKEN with actions:
# read and GITHUB_REPOSITORY (later runs).

set -euo pipefail
# shellcheck source=tether-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/tether-env.sh"

bookmark="${1:?usage: tether-fork.sh <bookmark>}"
out="${RUNNER_TEMP:-/tmp}/tether-tfvars.json"

if dgit ls-remote --exit-code --heads origin "$bookmark" >/dev/null; then
  run=$(gh api "repos/$GITHUB_REPOSITORY/actions/artifacts?name=tether-fork-$bookmark&per_page=100" \
    --jq '[.artifacts[] | select(.expired | not)] | sort_by(.created_at) | last | .workflow_run.id // empty')
  if [ -z "$run" ]; then
    echo "tether-fork: $bookmark is already forked, but no run left its addresses (artifact tether-fork-$bookmark); remove and re-add the preview label for a fresh fork" >&2
    exit 1
  fi
  saved=$(mktemp -d)
  gh run download "$run" -R "$GITHUB_REPOSITORY" -n "tether-fork-$bookmark" -D "$saved"
  cp "$saved/tether-tfvars.json" "$out"
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
