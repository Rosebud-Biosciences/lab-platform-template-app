#!/usr/bin/env bash
#
# The program of main.tf's external data source "neon_parent": reports the
# Neon branch a preview's database branch is made from, and whether it is
# protected. Neon gives a branch made from a protected branch new passwords
# for its roles; main.tf's precondition refuses to stand a preview up on any
# other.
#
# Query on stdin: {project_id, branch_id, fork}. With fork "true", branch_id
# is the preview's own branch (a tether fork) and its parent is looked up;
# otherwise branch_id is the parent (tofu mode, before the branch exists).
# Prints {id, name, protected}. Exits non-zero only when Neon cannot be read:
# `tofu destroy` reads this too, and must not fail on an unprotected parent.
#
# The API key comes from the environment (NEON_API_KEY, or the
# TF_VAR_neon_api_key the provider reads), never the query, so it stays out of
# the state. Needs curl and jq.

set -euo pipefail

query=$(cat)
project=$(jq -r '.project_id' <<<"$query")
branch=$(jq -r '.branch_id' <<<"$query")
fork=$(jq -r '.fork' <<<"$query")
key="${NEON_API_KEY:-${TF_VAR_neon_api_key:-}}"
[ -n "$key" ] || {
  echo "neon-protected: set NEON_API_KEY" >&2
  exit 1
}

get_branch() {
  curl -fsS -H "Authorization: Bearer $key" -H "Accept: application/json" \
    "https://console.neon.tech/api/v2/projects/$project/branches/$1" | jq -e '.branch'
}

if [ "$fork" = true ]; then
  branch=$(get_branch "$branch" | jq -r '.parent_id // empty')
  [ -n "$branch" ] || {
    echo "neon-protected: the preview's branch has no parent" >&2
    exit 1
  }
fi
get_branch "$branch" | jq -c '{id, name, protected: (.protected == true | tostring)}'
