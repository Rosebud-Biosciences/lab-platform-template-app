"""One hello-world asset per data store: the backend matrix.

Each asset derives something small from the greetings table and writes it to
a different kind of store through that store's native library, addressed by
DATA_REFS (see the ``dataset`` package). Together they exercise every backend the
repo's tether dataset registers -- Icechunk, Iceberg, Lance, Delta, a file
prefix -- so a preview forks (or a fresh tofu-mode copy receives) each kind,
and the tether-matrix workflow has something real to write.

Read-only addresses (a pinned version or snapshot) are reported, not written:
the same asset runs against a fork branch in a preview, ``main`` in prod, and
an exact recorded state in a reproduction, and only the address changes.
"""

from __future__ import annotations

import hashlib
from datetime import date

import dagster as dg
import numpy as np
import pyarrow as pa
from dataset import DELTA_KEY, FILE_KEY, ICEBERG_KEY, ICECHUNK_KEY, LANCE_KEY, openers
from db.engine import get_engine
from db.models import Greeting
from sqlalchemy import func, select
from sqlalchemy.orm import Session

VECTOR_DIM = 8


def _counts_by_name() -> list[tuple[str, int]]:
    with Session(get_engine()) as session:
        rows = session.execute(
            select(Greeting.name, func.count()).group_by(Greeting.name).order_by(Greeting.name)
        ).all()
    return [(str(name), int(count)) for name, count in rows]


def _counts_by_day() -> list[tuple[date, int]]:
    with Session(get_engine()) as session:
        rows = session.execute(
            select(func.date(Greeting.created_at), func.count())
            .group_by(func.date(Greeting.created_at))
            .order_by(func.date(Greeting.created_at))
        ).all()
    # sqlite hands back the date as text; Postgres as a date.
    return [(date.fromisoformat(str(day)[:10]), int(count)) for day, count in rows]


def _toy_embedding(name: str) -> list[float]:
    """Deterministic stand-in for a model: VECTOR_DIM floats from a digest."""
    digest = hashlib.blake2b(name.encode(), digest_size=VECTOR_DIM).digest()
    return [b / 255.0 for b in digest]


@dg.asset(group_name="stores")
def greetings_counts_zarr() -> dg.MaterializeResult:
    """Per-name counts as a Zarr array in the Icechunk store (zarr/greetings)."""
    import zarr

    counts = _counts_by_name()
    session = openers.icechunk_session(ICECHUNK_KEY, writable=True)
    group = zarr.open_group(session.store, mode="a")
    array = group.create_array("count", shape=(len(counts),), dtype="int64", overwrite=True)
    array[:] = np.array([c for _, c in counts], dtype="int64")
    array.attrs["names"] = [n for n, _ in counts]
    snapshot = session.commit(f"greeting counts for {len(counts)} names")

    return dg.MaterializeResult(metadata={"names": len(counts), "snapshot_id": str(snapshot)})


DAILY_SCHEMA = pa.schema(
    [
        pa.field("day", pa.date32(), nullable=False),
        pa.field("count", pa.int64(), nullable=False),
        pa.field("as_of", pa.date32(), nullable=False),
    ]
)


@dg.asset(group_name="stores")
def greetings_daily_iceberg() -> dg.MaterializeResult:
    """Greetings per day, appended to the Iceberg table (lake/greetings_daily)."""
    table, r = openers.iceberg_table(ICEBERG_KEY, schema=DAILY_SCHEMA)
    if r.pinned:
        rows = openers.iceberg_scan(table, r).to_arrow().num_rows
        return dg.MaterializeResult(metadata={"read_only": True, "rows_at_pin": rows})

    today = date.today()
    arrow = pa.Table.from_pylist(
        [{"day": d, "count": c, "as_of": today} for d, c in _counts_by_day()],
        schema=DAILY_SCHEMA,
    )
    table.append(arrow, branch=r.branch)
    snapshot = table.refs()[r.branch].snapshot_id
    return dg.MaterializeResult(
        metadata={"appended": arrow.num_rows, "branch": r.branch, "snapshot_id": int(snapshot)}
    )


@dg.asset(group_name="stores")
def greetings_vectors_lance() -> dg.MaterializeResult:
    """A toy embedding per name, appended to the Lance dataset (vec/greetings)."""
    ds, r = openers.lance_dataset(LANCE_KEY)
    if r.pinned:
        rows = ds.count_rows() if ds is not None else 0
        return dg.MaterializeResult(metadata={"read_only": True, "rows_at_pin": rows})

    names = [n for n, _ in _counts_by_name()]
    arrow = pa.table(
        {
            "name": pa.array(names, type=pa.string()),
            "vector": pa.array(
                [_toy_embedding(n) for n in names], type=pa.list_(pa.float32(), VECTOR_DIM)
            ),
        }
    )
    written = openers.lance_write(LANCE_KEY, arrow)
    return dg.MaterializeResult(
        metadata={"appended": len(names), "version": int(written.version), "branch": r.branch}
    )


LOG_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("logged_on", pa.date32(), nullable=False),
    ]
)


@dg.asset(group_name="stores")
def greetings_log_delta() -> dg.MaterializeResult:
    """Every greeting row, appended to the Delta table (delta/greetings_log).

    Delta has versions but no branches, so a preview in tether mode gets a
    read-only ``@vN`` address and this asset reports the row count at that
    version instead of writing -- prod (and a tofu-mode copy) append.
    """
    from deltalake import DeltaTable, write_deltalake

    uri, r = openers.delta_location(DELTA_KEY)
    if r.version is not None:
        rows = DeltaTable(uri, version=r.version).to_pyarrow_table().num_rows
        return dg.MaterializeResult(
            metadata={"read_only": True, "version": r.version, "rows_at_version": rows}
        )

    with Session(get_engine()) as session:
        greetings = session.execute(select(Greeting.id, Greeting.name)).all()
    today = date.today()
    arrow = pa.Table.from_pylist(
        [{"id": int(i), "name": str(n), "logged_on": today} for i, n in greetings],
        schema=LOG_SCHEMA,
    )
    write_deltalake(uri, arrow, mode="append")
    version = DeltaTable(uri).version()
    return dg.MaterializeResult(metadata={"appended": arrow.num_rows, "version": int(version)})


@dg.asset(group_name="stores")
def raw_uploads_inventory() -> dg.MaterializeResult:
    """What sits under the raw uploads prefix (raw/uploads): read-only, always.

    A ``file`` object is fingerprinted, never forked: its drift is an error
    tether's nightly ``status`` raises, so this asset only takes inventory.
    """
    entries = openers.list_prefix(FILE_KEY)
    return dg.MaterializeResult(
        metadata={
            "files": len(entries),
            "bytes": sum(size for _, size in entries),
            "sample": [path for path, _ in entries[:10]],
        }
    )


STORE_ASSETS = [
    greetings_counts_zarr,
    greetings_daily_iceberg,
    greetings_vectors_lance,
    greetings_log_delta,
    raw_uploads_inventory,
]

data_stores_job = dg.define_asset_job("data_stores_job", selection=STORE_ASSETS)
