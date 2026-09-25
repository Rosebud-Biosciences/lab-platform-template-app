#!/usr/bin/env bash
#
# Write, as one JSON object, everything the preview stack needs from the tether
# bookmark the dataset checkout is on -- and nothing secret:
#
#   fork_dbs    every neon object on the fork (db/app, then db/<svc>:
#               Dagster's run storage, MLflow's tracking store, Argo's
#               workflow archive), keyed by the name after db/, as
#               {project_id, branch_id, host, dbname, user}. No password: the
#               stack reads each role's from Neon (neon_branch_role_password),
#               because GitHub drops a job output that holds a masked value in
#               any encoding. Registering another db/<svc> manifest adds a key
#               here with no script change.
#   data_refs   DATA_REFS: every non-database object -> address on the fork,
#               exactly as `tether open --json` prints it
#
# That object is what CI hands the reusable preview-up workflow as
# extra_tfvars_json, and what the matrix reads DATA_REFS from.
#
#   tether-open-all.sh <out.json>
#
# Run from the repo root after `tether new -b <bookmark> --eager` (see
# tether-env.sh for DATASET_ROOT), with NEON_API_KEY set: each branch ID comes
# from Neon's API, by the endpoint tether's URL names. Needs uv-synced venv, jq,
# curl, python3.

set -euo pipefail
# shellcheck source=tether-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/tether-env.sh"

out="${1:?usage: tether-open-all.sh <out.json>}"
: "${NEON_API_KEY:?tether-open-all: NEON_API_KEY is needed to look up the fork branches}"

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
      # Writable so the read_write endpoint exists before pods connect;
      # without --with-password tether redacts the URL's password.
      url=$(tether open "$key" --writable)
      project=$(sed -n 's/^project_id *= *"\([^"]*\)".*/\1/p' "$objects/$key.toml")
      db=$(python3 -c '
import json, sys, urllib.parse as u
d = u.urlsplit(sys.argv[1])
print(json.dumps({"host": d.hostname or "", "dbname": (d.path or "/").lstrip("/"), "user": u.unquote(d.username or "")}))
' "$url")
      host=$(jq -r '.host' <<<"$db")
      endpoint="${host%%.*}"
      endpoint="${endpoint%-pooler}"
      branch=$(curl -fsS -H "Authorization: Bearer $NEON_API_KEY" -H "Accept: application/json" \
        "https://console.neon.tech/api/v2/projects/$project/endpoints/$endpoint" | jq -r '.endpoint.branch_id // empty')
      if [ -z "$project" ] || [ -z "$host" ] || [ -z "$branch" ]; then
        echo "tether-open-all: cannot place $key on its fork (project '$project', host '$host', branch '$branch')" >&2
        exit 1
      fi
      dbs=$(jq -c --arg k "${key#db/}" --arg p "$project" --arg b "$branch" --argjson d "$db" \
        '. + {($k): ($d + {project_id: $p, branch_id: $b})}' <<<"$dbs")
      ;;
    *)
      address=$(tether open "$key" --json | jq -r '.address')
      refs=$(jq -c --arg k "$key" --arg v "$address" '. + {($k): $v}' <<<"$refs")
      ;;
  esac
done <<<"$keys"

jq -e 'has("app")' <<<"$dbs" >/dev/null || {
  echo "tether-open-all: no db/app object in the dataset (the webapp's DATABASE_URL)" >&2
  exit 1
}
jq -n --argjson dbs "$dbs" --argjson refs "$refs" \
  '{fork_dbs: $dbs, data_refs: ($refs | to_entries | sort_by(.key) | from_entries | tojson)}' >"$out"
