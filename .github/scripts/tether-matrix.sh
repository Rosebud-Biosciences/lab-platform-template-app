#!/usr/bin/env bash
#
# tether's live test against the real services: fork every registered store,
# write to each fork through the app's own assets, pin, diff, plan a promote,
# then release it all. tether's Neon and Iceberg backends are `experimental`
# ("tested against a fake of the service, not the service itself"); this is
# the run against the service itself, on this repo's budget.
#
# Usage: tether-matrix.sh            # from the repo root; DATASET_ROOT per tether-env.sh
#
# Writes a per-backend markdown report to $REPORT (default matrix-report.md)
# and exits 2 when any backend failed, 0 when all passed, 1 on its own error
# -- the tofu plan -detailed-exitcode convention the drift workflows use.
#
# Nothing here touches production `main` branches: writes go to the fork,
# `promote` runs with --dry-run only, and cleanup releases the fork. Needs
# NEON_API_KEY, AWS credentials holding the data-access policy, uv, jq.

set -euo pipefail
# shellcheck source=tether-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/tether-env.sh"

report="${REPORT:-matrix-report.md}"
bookmark="matrix-${GITHUB_RUN_ID:-local-$$}"
steps=()
failed=0

note() { steps+=("$1"); echo "tether-matrix: $1"; }

cleanup() {
  # Always release the fork, even after a failure: branches in six stores
  # would otherwise outlive the run. The bookmark's commit becomes unreachable
  # once the branch is deleted, so gc also releases the pins it made. Runs as
  # the EXIT trap, appends its outcome to the report, and sets the final exit
  # code: 0 all passed, 2 something failed (the drift workflows' convention).
  dgit checkout --quiet - 2>/dev/null || true
  dgit branch -D "$bookmark" >/dev/null 2>&1 || true
  if tether gc --prune-bookmarks --force-prune --no-dry-run; then
    line="gc: released $bookmark's branches and pins"
  else
    line="gc: FAILED to release $bookmark -- run tether gc --prune-bookmarks --force-prune by hand"
    failed=1
  fi
  echo "tether-matrix: $line"
  [ -f "$report" ] && printf '\n### Cleanup\n\n- %s\n' "$line" >> "$report"
  exit $((failed ? 2 : 0))
}
trap cleanup EXIT

# 1. Fork.
if tether new -b "$bookmark" --eager; then
  note "new --eager: forked every store onto $bookmark"
else
  note "new --eager: FAILED"
  echo "## tether matrix: fork failed" > "$report"
  printf -- '- %s\n' "${steps[@]}" >> "$report"
  failed=1
  exit 0 # the EXIT trap turns failed into the real code
fi

# 2. Addresses for the assets: the fork's database and DATA_REFS, plus the
#    Iceberg catalog tether itself uses, as JSON for pyiceberg: tether.toml's
#    type and warehouse over the endpoint and signing tether-env.sh exported
#    for the catalog name the manifests use (the app's openers read neither).
opened_file="${RUNNER_TEMP:-/tmp}/matrix-opened.json"
.github/scripts/tether-open-all.sh "$opened_file"
opened=$(cat "$opened_file")
export DATABASE_URL
DATABASE_URL=$(jq -r '.database_url' <<<"$opened")
export DATA_REFS
DATA_REFS=$(jq -r '.data_refs' <<<"$opened")
export ICEBERG_CATALOG
ICEBERG_CATALOG=$(uv run --frozen python -c '
import json, sys, tomllib
from pyiceberg.utils.config import Config
committed = tomllib.load(open(sys.argv[1], "rb"))["backends"]["iceberg"]["catalog"]
print(json.dumps({**(Config().get_catalog_config("s3tables") or {}), **committed}))
' "$DATASET_ROOT/tether.toml")
note "open: addresses for $(jq -r 'fromjson | keys | join(", ")' <<<"$DATA_REFS")"

# 3. Write through the app's own assets, one backend at a time.
rows="${RUNNER_TEMP:-/tmp}/matrix-rows.json"
if uv run --frozen python .github/scripts/tether_matrix.py > "$rows"; then
  note "assets: every backend wrote to its fork"
else
  note "assets: at least one backend failed (table below)"
  failed=1
fi

# 4. Pin, diff, and plan the landing (dry run: this never lands on prod).
if tether commit -m "matrix $bookmark" --force; then
  note "commit: pinned the fork heads"
else
  note "commit: FAILED"
  failed=1
fi
diff_out=$(tether diff main --content 2>&1 || true)
promote_out=$(tether promote --dry-run 2>&1 || true)

# 5. Report.
{
  echo "## tether matrix: $bookmark"
  echo
  echo "| Backend | Object | Asset | Result | Detail |"
  echo "| --- | --- | --- | --- | --- |"
  jq -r '.[] | "| \(.backend) | `\(.key)` | \(.asset) | \(if .status == "pass" then "pass" else "**fail**" end) | \(.detail | tostring | .[0:160] | gsub("\\|"; "\\\\|")) |"' "$rows"
  echo
  echo "### Lifecycle"
  echo
  printf -- '- %s\n' "${steps[@]}"
  echo
  echo "### tether diff main --content"
  echo
  echo '```'
  echo "$diff_out"
  echo '```'
  echo
  echo "### tether promote --dry-run (never applied)"
  echo
  echo '```'
  echo "$promote_out"
  echo '```'
} > "$report"
# Exit code comes from the EXIT trap (cleanup), which knows whether gc worked.
