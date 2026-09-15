"""One native opener per store kind, addressed by DATA_REFS.

Each function resolves a key through :mod:`dataset.refs` and hands back the
store's own handle -- an Icechunk session, a pyiceberg table, a Lance
dataset, a Delta location, an object listing. tether's stance is the same:
it does not sit in the data path, the native library does. Needs the
``dataset[stores]`` extra; imports are lazy so the contract module stays
importable without them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dataset.refs import Ref, data_root, local_iceberg_catalog, ref

# ---------------------------------------------------------------------------
# Icechunk (Zarr)
# ---------------------------------------------------------------------------


def icechunk_repository(r: Ref) -> Any:
    import icechunk as ic

    if r.is_local:
        Path(r.local_path).mkdir(parents=True, exist_ok=True)
        storage = ic.local_filesystem_storage(r.local_path)
    else:
        u = urlparse(r.location)
        storage = ic.s3_storage(
            bucket=u.netloc,
            prefix=u.path.lstrip("/"),
            region=os.environ.get("AWS_REGION"),
            from_env=True,
        )
    return ic.Repository.open_or_create(storage)


def icechunk_session(key: str, *, writable: bool) -> Any:
    """A Zarr-ready Icechunk session on the address' branch (or pinned state)."""
    r = ref(key)
    repo = icechunk_repository(r)
    if r.pinned:
        if writable:
            raise PermissionError(f"{key}: {r.ref} is a pinned state, not a branch")
        if r.ref is not None and r.ref.startswith("tether."):
            return repo.readonly_session(tag=r.ref)
        return repo.readonly_session(snapshot_id=r.ref)
    branch = r.branch
    if branch != "main" and branch not in repo.list_branches():
        raise LookupError(
            f"{key}: branch {branch} does not exist in {r.location}; the fork "
            "step (tether new --eager) creates it before pods start"
        )
    return repo.writable_session(branch) if writable else repo.readonly_session(branch=branch)


# ---------------------------------------------------------------------------
# Iceberg (pyiceberg)
# ---------------------------------------------------------------------------


def iceberg_catalog() -> Any:
    """The catalog from ICEBERG_CATALOG (pyiceberg properties as JSON), else a
    local sqlite one under DATA_ROOT -- the same fallback rule as DATA_REFS."""
    from pyiceberg.catalog import load_catalog

    raw = os.environ.get("ICEBERG_CATALOG")
    props = json.loads(raw) if raw else local_iceberg_catalog(data_root())
    if props.get("type") == "sql":
        Path(urlparse(props["warehouse"]).path).mkdir(parents=True, exist_ok=True)
    return load_catalog(props.pop("name", "default"), **props)


def iceberg_table(key: str, schema: Any) -> tuple[Any, Ref]:
    """Load (creating if absent) the table an address names, plus the parsed ref.

    Creating happens only where a fresh store is expected (tofu mode's empty
    namespace, a laptop). In tether mode the table and its fork branch exist
    before pods start, so a missing branch is reported rather than invented.
    """
    r = ref(key)
    catalog = iceberg_catalog()
    namespace = r.location.rsplit(".", 1)[0]
    catalog.create_namespace_if_not_exists(namespace)
    table = catalog.create_table_if_not_exists(r.location, schema=schema)
    if r.branch != "main" and r.branch not in table.refs():
        raise LookupError(
            f"{key}: branch {r.branch} does not exist on {r.location}; the fork "
            "step (tether new --eager) creates it before pods start"
        )
    return table, r


def iceberg_scan(table: Any, r: Ref) -> Any:
    """A scan of the address' state: a branch head or a pinned snapshot."""
    if r.pinned and r.ref is not None:
        return table.scan(snapshot_id=int(r.ref))
    snapshot = table.refs().get(r.branch)
    return table.scan(snapshot_id=snapshot.snapshot_id) if snapshot else table.scan()


# ---------------------------------------------------------------------------
# Lance
# ---------------------------------------------------------------------------


def lance_dataset(key: str) -> tuple[Any | None, Ref]:
    """The dataset checked out at the address' branch (or version), or None if
    it does not exist yet (a fresh tofu-mode store)."""
    import lance

    r = ref(key)
    try:
        ds = lance.dataset(r.local_path if r.is_local else r.location)
    except ValueError, OSError:
        return None, r
    if r.branch == "main":
        return (ds.checkout_version(r.version) if r.version is not None else ds), r
    return ds.checkout_version((r.branch, r.version)), r


def lance_write(key: str, data: Any) -> Any:
    """Append to the address' branch, creating the dataset when it is new."""
    import lance

    ds, r = lance_dataset(key)
    if r.pinned:
        raise PermissionError(f"{key}: {r.location}#{r.ref}@v{r.version} is pinned, not a branch")
    if ds is None:
        if r.branch != "main":
            raise LookupError(f"{key}: {r.location} does not exist, so branch {r.branch} cannot")
        return lance.write_dataset(data, r.local_path if r.is_local else r.location, mode="create")
    return lance.write_dataset(data, ds, mode="append")


# ---------------------------------------------------------------------------
# Delta Lake
# ---------------------------------------------------------------------------


def delta_location(key: str) -> tuple[str, Ref]:
    r = ref(key)
    return (r.local_path if r.is_local else r.location), r


# ---------------------------------------------------------------------------
# Files / object-store prefixes
# ---------------------------------------------------------------------------


def list_prefix(key: str) -> list[tuple[str, int]]:
    """(path, size) for every object under the address' prefix; read-only."""
    from obstore.store import from_url

    r = ref(key)
    if r.is_local:
        root = Path(r.local_path)
        root.mkdir(parents=True, exist_ok=True)
        return sorted(
            (str(p.relative_to(root)), p.stat().st_size) for p in root.rglob("*") if p.is_file()
        )
    u = urlparse(r.location)
    store = from_url(f"{u.scheme}://{u.netloc}", region=os.environ.get("AWS_REGION"))
    prefix = u.path.lstrip("/")
    entries: list[tuple[str, int]] = []
    for batch in store.list(prefix):
        entries.extend((str(o["path"]), int(o["size"])) for o in batch)
    return sorted(entries)
