"""Materialize each store asset on its own and report per backend.

Run by tether-matrix.sh with DATABASE_URL and DATA_REFS pointing at a tether
fork (a `matrix-<run>` bookmark). Each asset is one backend's write path; a
failure in one must not hide the others, so they run separately and the
result is one JSON row per backend on stdout.
"""

from __future__ import annotations

import json
import sys
import traceback

import dagster as dg
from dagster._core.events import StepFailureData
from workflows import stores

BACKEND_OF = {
    stores.greetings_counts_zarr: ("icechunk", "zarr/greetings"),
    stores.greetings_daily_iceberg: ("iceberg", "lake/greetings_daily"),
    stores.greetings_vectors_lance: ("lance", "vec/greetings"),
    stores.greetings_log_delta: ("delta", "delta/greetings_log"),
    stores.raw_uploads_inventory: ("file", "raw/uploads"),
}


def main() -> int:
    rows = []
    failures = 0
    for asset, (backend, key) in BACKEND_OF.items():
        name = asset.key.to_user_string()
        try:
            result = dg.materialize([asset], raise_on_error=False)
            if result.success:
                meta = result.asset_materializations_for_node(name)[0].metadata
                rows.append(
                    {
                        "backend": backend,
                        "key": key,
                        "asset": name,
                        "status": "pass",
                        "detail": {k: str(v.value) for k, v in meta.items()},
                    }
                )
            else:
                failures += 1
                errors = []
                for event in result.get_step_failure_events():
                    failure = event.event_specific_data
                    if isinstance(failure, StepFailureData) and failure.error is not None:
                        errors.append(failure.error.message)
                rows.append(
                    {
                        "backend": backend,
                        "key": key,
                        "asset": name,
                        "status": "fail",
                        "detail": errors,
                    }
                )
        except Exception:  # noqa: BLE001 -- the report is the point
            failures += 1
            rows.append(
                {
                    "backend": backend,
                    "key": key,
                    "asset": name,
                    "status": "fail",
                    "detail": traceback.format_exc().splitlines()[-3:],
                }
            )
    json.dump(rows, sys.stdout, indent=2)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
