#!/usr/bin/env bash
#
# Fork every registered store for one preview and hand the addresses to the
# stack. Runs in preview-up.yml's fork-data job (tether mode only).
#
# Usage: tether-fork.sh <bookmark>          # e.g. pr123
#
# `tether new -b <bookmark> --eager` creates the bookmark at the checked-out
# commit of the dataset and, because tether.toml says eager, one branch per
# store right now (tether.ws.<dataset>.<bookmark>), forked from the last pinned
# state on main. On a PR's later pushes the same command runs again: tether
# RESETS the existing store branches onto those pins, so every revision of the
# PR is tested against a fresh fork of the baseline. (tofu mode keeps a
# preview's data across pushes; this is the one behavioural difference, and
# the README says so.)
#
# The bookmark is pushed as a git branch of the repository that holds the
# dataset (this one, or the dataset repo when DATASET_ROOT is a submodule) so
# preview-down.yml and sweep.yml can see which previews still hold forks from
# any checkout -- see tether-down.sh for why that matters to `gc`.
#
# Output: tfvars_b64 in $GITHUB_OUTPUT -- base64 of the JSON object
# tether-open-all.sh prints, for the reusable workflow's extra_tfvars_json.

set -euo pipefail
# shellcheck source=tether-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/tether-env.sh"

bookmark="${1:?usage: tether-fork.sh <bookmark>}"

tether new -b "$bookmark" --eager
dgit push --force --quiet origin "HEAD:refs/heads/$bookmark"

out="${RUNNER_TEMP:-/tmp}/tether-tfvars.json"
.github/scripts/tether-open-all.sh > "$out"
echo "forked $(jq -r '.data_refs | fromjson | keys | join(", ")' "$out") plus db/app, db/dagster onto $bookmark"
echo "tfvars_b64=$(base64 < "$out" | tr -d '\n')" >> "${GITHUB_OUTPUT:-/dev/stdout}"
