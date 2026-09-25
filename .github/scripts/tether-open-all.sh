#!/usr/bin/env bash
#
# Write, as one JSON object, everything the preview stack needs from the tether
# bookmark the dataset checkout is on:
#
#   database_url         db/app on the fork, with password
#   service_dbs          db/<svc> on the fork for every other neon object,
#                        split into {host, dbname, user, password} keyed by
#                        <svc> -- Dagster's run storage, MLflow's tracking
#                        store, Argo's workflow archive; the stack hands each
#                        to the matching <svc>_db_* inputs of the workloads
#                        module. Registering another db/<svc> manifest adds a
#                        key here with no script change.
#   data_refs            DATA_REFS: every non-database object -> address on the
#                        fork, exactly as `tether open --json` prints it
#
# That object is what CI hands the reusable preview-up workflow as
# extra_tfvars_json (base64: a job output containing a masked value is dropped
# by GitHub) and what the matrix workflow exports for the assets.
#
#   tether-open-all.sh <out.json>
#
# The object goes to the file, never stdout: under GitHub Actions stdout
# carries the `::add-mask::` commands for the fork's passwords, which only mask
# them if the runner reads them, and which would corrupt the JSON if they
# shared its stream. Outside Actions nothing is printed.
#
# Run from the repo root after `tether new -b <bookmark> --eager` (see
# tether-env.sh for DATASET_ROOT). Needs uv-synced venv, jq, python3.

set -euo pipefail
# shellcheck source=tether-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/tether-env.sh"

out="${1:?usage: tether-open-all.sh <out.json>}"

objects="$DATASET_ROOT/.tether/objects"
keys=$(find "$objects" -name '*.toml' | sed "s#^$objects/##; s#\.toml\$##" | sort)

refs='{}'
dbs='{}'
while IFS= read -r key; do
  [ -n "$key" ] || continue
  kind=$(sed -n 's/^kind *= *"\([^"]*\)".*/\1/p' "$objects/$key.toml")
  case "$kind" in
    neon)
      case "$key" in
        db/*) ;;
        *) echo "tether-open-all: neon object $key must be keyed db/<name> (db/app or a service's database)" >&2; exit 1 ;;
      esac
      # Writable so the read_write endpoint exists before pods connect.
      url=$(tether open "$key" --writable --with-password)
      password=$(python3 -c 'import sys, urllib.parse as u; print(u.urlsplit(sys.argv[1]).password or "")' "$url")
      if [ -n "$password" ] && [ "${GITHUB_ACTIONS:-}" = "true" ]; then
        echo "::add-mask::$password"
      fi
      dbs=$(jq -c --arg k "${key#db/}" --arg v "$url" '. + {($k): $v}' <<<"$dbs")
      ;;
    *)
      address=$(tether open "$key" --json | jq -r '.address')
      refs=$(jq -c --arg k "$key" --arg v "$address" '. + {($k): $v}' <<<"$refs")
      ;;
  esac
done <<<"$keys"

python3 - "$dbs" "$refs" "$out" <<'EOF'
import json, sys, urllib.parse as u

dbs, refs, out = json.loads(sys.argv[1]), json.loads(sys.argv[2]), sys.argv[3]
if "app" not in dbs:
    sys.exit("tether-open-all: no db/app object in the dataset (the webapp's DATABASE_URL)")


def split(url: str) -> dict:
    d = u.urlsplit(url)
    return {
        "host": d.hostname or "",
        "dbname": (d.path or "/").lstrip("/"),
        "user": u.unquote(d.username or ""),
        "password": u.unquote(d.password or ""),
    }


with open(out, "w") as f:
    json.dump({
        "database_url": dbs["app"],
        "service_dbs": {svc: split(url) for svc, url in sorted(dbs.items()) if svc != "app"},
        "data_refs": json.dumps(refs, sort_keys=True),
    }, f)
EOF
