#!/usr/bin/env bash
# Fail if any path in .github/template-drift/shared-paths differs from the
# template -- except the dataset's deployment values (deployment-values.sed),
# which both sides have masked before the comparison, and what this
# deployment's data has recorded: each manifest's captured_at, recoverable and
# [state] / [pin] tables (data-pull moves them nightly) and the file objects'
# listings.
#
#   .github/template-drift/check.sh <template checkout>
set -euo pipefail

template="${1:?usage: $0 <template checkout>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# stage <tree> <side> <path>: copy one side's path into the work tree with
# the dataset manifests' deployment values rewritten to placeholders, their
# recorded states and blank lines dropped, and the listings left out.
stage() {
  [ -e "$1/$3" ] || return 0
  mkdir -p "$work/$2/$(dirname "$3")"
  cp -R "$1/$3" "$work/$2/$3"
  find "$work/$2" -type d -path "*/packages/dataset/.tether/listings" -prune -exec rm -rf {} +
  find "$work/$2" -type f \( -path "*/packages/dataset/tether.toml" -o -path "*/packages/dataset/.tether/objects/*.toml" \) |
    while IFS= read -r f; do
      sed -E -f "$here/deployment-values.sed" "$f" |
        awk '/^\[/ { skip = ($0 ~ /^\[(state|pin)[].]/) } skip || /^(captured_at|recoverable) = / || /^[[:space:]]*$/ { next } { print }' >"$f.masked"
      mv "$f.masked" "$f"
    done
}

drift=0
while IFS= read -r path; do
  case "$path" in "" | \#*) continue ;; esac
  stage "$template" template "$path"
  stage "$root" here "$path"
  if ! diff -r --exclude=__pycache__ --exclude='*.egg-info' "$work/template/$path" "$work/here/$path" >"$work/drift.diff" 2>&1; then
    echo "::error file=$path::differs from the template"
    sed -e "s#$work/##g" "$work/drift.diff" | sed -n '1,40p'
    drift=1
  fi
done <"$here/shared-paths"

if [ "$drift" -ne 0 ]; then
  echo "Copy the template's version (fixes land there first), or drop the path from .github/template-drift/shared-paths if this repo now owns it."
  exit 1
fi
echo "no drift from the template"
