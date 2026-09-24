"""The backend matrix against local stores: no services, no credentials.

DATA_REFS is built by ``dataset.local_refs`` under a temp dir (exactly what a
laptop run without DATA_REFS does), and the Iceberg catalog is sqlite. The
same assets run in a preview and prod; only the addresses differ.
"""

import importlib
import json
from pathlib import Path

import dagster as dg
import numpy as np
import pytest
from dataset import (
    DELTA_KEY,
    FILE_KEY,
    ICEBERG_KEY,
    ICECHUNK_KEY,
    LANCE_KEY,
    local_iceberg_catalog,
    local_refs,
    openers,
    ref,
    refs,
)
from db.engine import get_engine
from db.models import Base, Greeting
from sqlalchemy.orm import Session
from workflows.stores import (
    DAILY_SCHEMA,
    STORE_ASSETS,
    greetings_counts_zarr,
    greetings_daily_iceberg,
    greetings_log_delta,
    greetings_vectors_lance,
    raw_uploads_inventory,
)


@pytest.fixture
def local_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/wf.db")
    monkeypatch.setenv("DATA_REFS", json.dumps(local_refs(tmp_path / "stores")))
    monkeypatch.setenv("ICEBERG_CATALOG", json.dumps(local_iceberg_catalog(tmp_path)))
    engine = get_engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(Greeting(name=n) for n in ["ada", "ada", "grace"])
        session.commit()
    return tmp_path


def _meta(result: dg.ExecuteInProcessResult, asset: str) -> dict:
    return {
        k: v.value for k, v in result.asset_materializations_for_node(asset)[0].metadata.items()
    }


def test_zarr_counts_land_in_icechunk(local_data: Path) -> None:
    import zarr

    result = dg.materialize([greetings_counts_zarr])
    assert result.success
    meta = _meta(result, "greetings_counts_zarr")
    assert meta["names"] == 2

    group = zarr.open_group(openers.icechunk_session(ICECHUNK_KEY, writable=False).store, mode="r")
    count = group["count"]
    assert isinstance(count, zarr.Array)
    assert np.asarray(count[:]).tolist() == [2, 1]
    assert count.attrs["names"] == ["ada", "grace"]


def test_iceberg_appends_on_the_branch(local_data: Path) -> None:
    result = dg.materialize([greetings_daily_iceberg])
    assert result.success
    meta = _meta(result, "greetings_daily_iceberg")
    assert meta["appended"] == 1 and meta["branch"] == "main"

    table, r = openers.iceberg_table(ICEBERG_KEY, schema=DAILY_SCHEMA)
    assert openers.iceberg_scan(table, r).to_arrow().num_rows == 1


def test_iceberg_opens_what_exists_without_creating(
    local_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyiceberg.catalog.sql import SqlCatalog

    created, _ = openers.iceberg_table(ICEBERG_KEY, schema=DAILY_SCHEMA)

    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError("S3 Tables refuses a create the role is not granted")

    monkeypatch.setattr(SqlCatalog, "create_namespace", refuse)
    monkeypatch.setattr(SqlCatalog, "create_table", refuse)
    opened, _ = openers.iceberg_table(ICEBERG_KEY, schema=DAILY_SCHEMA)
    assert opened.name() == created.name()


def test_the_s3_tables_catalog_can_sign_requests() -> None:
    # The sqlite catalog above never signs; prod's S3 Tables REST catalog
    # imports boto3 for SigV4 only once it first connects.
    importlib.import_module("boto3")


def test_lance_creates_then_appends(local_data: Path) -> None:
    first = dg.materialize([greetings_vectors_lance])
    second = dg.materialize([greetings_vectors_lance])
    assert first.success and second.success
    assert _meta(second, "greetings_vectors_lance")["version"] == 2

    ds, _ = openers.lance_dataset(LANCE_KEY)
    assert ds is not None and ds.count_rows() == 4


def test_delta_appends_or_reads_a_pinned_version(
    local_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = dg.materialize([greetings_log_delta])
    assert result.success
    assert _meta(result, "greetings_log_delta") == {"appended": 3, "version": 0}

    # A tether-mode preview sees Delta as an exact version: report, don't write.
    refs_map = refs()
    refs_map[DELTA_KEY] = f"{refs_map[DELTA_KEY]}@v0"
    monkeypatch.setenv("DATA_REFS", json.dumps(refs_map))
    pinned = dg.materialize([greetings_log_delta])
    assert pinned.success
    assert _meta(pinned, "greetings_log_delta") == {
        "read_only": True,
        "version": 0,
        "rows_at_version": 3,
    }


def test_inventory_lists_the_prefix(local_data: Path) -> None:
    uploads = Path(ref(FILE_KEY).location)
    uploads.mkdir(parents=True)
    (uploads / "plate1.csv").write_text("a,b\n1,2\n")

    result = dg.materialize([raw_uploads_inventory])
    assert result.success
    meta = _meta(result, "raw_uploads_inventory")
    assert (meta["files"], meta["bytes"]) == (1, 8)


def test_every_store_asset_materializes_together(local_data: Path) -> None:
    assert dg.materialize(STORE_ASSETS).success
