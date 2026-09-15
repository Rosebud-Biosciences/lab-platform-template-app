#!/usr/bin/env bash
#
# Print, as one JSON object, everything the preview stack needs from the tether
# bookmark the dataset checkout is on:
#
#   database_url         db/app on the fork, with password
#   dagster_db_host/name/user/password
#                        db/dagster on the fork, split for the workloads module
#   data_refs            DATA_REFS: every non-database object -> address on the
#                        fork, exactly as `tether open --json` prints it
#
# That object is what CI hands the reusable preview-up workflow as
# extra_tfvars_json (base64: a job output containing a masked value is dropped
# by GitHub) and what the matrix workflow exports for the assets. Passwords are
# masked in the Actions log before anything else is printed.
#
# Run from the repo root after `tether new -b <bookmark> --eager` (see
# tether-env.sh for DATASET_ROOT). Needs uv-synced venv, jq, python3.

set -euo pipefail
# shellcheck source=tether-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/tether-env.sh"

objects="$DATASET_ROOT/.tether/objects"
keys=$(find "$objects" -name '*.toml' | sed "s#^$objects/##; s#\.toml\$##" | sort)

refs='{}'
database_url=""
dagster_url=""
while IFS= read -r key; do
  [ -n "$key" ] || continue
  kind=$(sed -n 's/^kind *= *"\([^"]*\)".*/\1/p' "$objects/$key.toml")
  case "$kind" in
    neon)
      # Writable so the read_write endpoint exists before pods connect.
      url=$(tether open "$key" --writable --with-password)
      password=$(python3 -c 'import sys, urllib.parse as u; print(u.urlsplit(sys.argv[1]).password or "")' "$url")
      [ -z "$password" ] || echo "::add-mask::$password"
      case "$key" in
        db/app) database_url="$url" ;;
        db/dagster) dagster_url="$url" ;;
        *) echo "tether-open-all: unexpected neon object $key (expected db/app, db/dagster)" >&2; exit 1 ;;
      esac
      ;;
    *)
      address=$(tether open "$key" --json | jq -r '.address')
      refs=$(jq -c --arg k "$key" --arg v "$address" '. + {($k): $v}' <<<"$refs")
      ;;
  esac
done <<<"$keys"

python3 - "$database_url" "$dagster_url" "$refs" <<'EOF'
import json, sys, urllib.parse as u
database_url, dagster_url, refs = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
d = u.urlsplit(dagster_url) if dagster_url else None
print(json.dumps({
    "database_url": database_url,
    "dagster_db_host": d.hostname or "" if d else "",
    "dagster_db_name": (d.path or "/").lstrip("/") if d else "",
    "dagster_db_user": u.unquote(d.username or "") if d else "",
    "dagster_db_password": u.unquote(d.password or "") if d else "",
    "data_refs": json.dumps(refs, sort_keys=True),
}))
EOF
