"""The DATA_REFS contract: which data objects exist and where each one is.

Every data object this app touches is registered in the dataset's tether
manifests (``packages/dataset/.tether/objects/<key>.toml``). At run time the
code does not consult those manifests; it receives **DATA_REFS**, a JSON
object ``{key: address}``, in the address forms ``tether open --json`` prints::

    zarr/greetings       s3://bucket/tether/greetings.icechunk#<branch|tag|snapshot>
    lake/greetings_daily lake.greetings_daily#<branch>        (+ ICEBERG_CATALOG)
    vec/greetings        s3://bucket/tether/greetings.lance#<branch>[@vN]
    delta/greetings_log  s3://bucket/tether/greetings_log.delta[@vN]
    raw/uploads          s3://bucket/raw/uploads/

Who builds the map is the whole point: the tofu preview stack roots it at the
preview's ephemeral bucket and namespace (fresh stores, ``#main``); CI's
tether fork job points it at ``tether.ws.<dataset>.pr<N>`` branches inside the
production stores; prod sets ``#main`` at the real locations; and with no
DATA_REFS at all (a laptop, compose, the tests) everything lands under
DATA_ROOT on the local filesystem. The consumers never know which.

A ``@vN`` suffix, or a tag / snapshot id after ``#``, means "this exact state":
the openers hand back read-only handles and callers report instead of write.

The key constants below are the code's copy of the manifest list;
``tests/test_manifests.py`` fails the moment the two (or the tofu stack's
``infra/preview/data.tf``) disagree.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# The Postgres objects travel as DATABASE_URL, not in DATA_REFS.
NEON_KEYS = ("db/app", "db/dagster")

ICECHUNK_KEY = "zarr/greetings"
ICEBERG_KEY = "lake/greetings_daily"
LANCE_KEY = "vec/greetings"
DELTA_KEY = "delta/greetings_log"
FILE_KEY = "raw/uploads"

# Every key DATA_REFS must carry.
KEYS = (ICECHUNK_KEY, ICEBERG_KEY, LANCE_KEY, DELTA_KEY, FILE_KEY)

_VERSION_SUFFIX = re.compile(r"^(?P<head>.*)@v(?P<version>\d+)$")


@dataclass(frozen=True)
class Ref:
    """One parsed DATA_REFS address."""

    key: str
    location: str  # a URI / path, or an Iceberg identifier
    ref: str | None  # what followed '#': a branch, tag, or snapshot id
    version: int | None  # a Delta version or Lance version pin (read-only)

    @property
    def branch(self) -> str:
        return self.ref or "main"

    @property
    def pinned(self) -> bool:
        """True when the address names an exact state rather than a branch head."""
        return self.version is not None or (
            self.ref is not None and not _looks_like_branch(self.ref)
        )

    @property
    def is_local(self) -> bool:
        return "://" not in self.location or self.location.startswith("file://")

    @property
    def local_path(self) -> str:
        return self.location.removeprefix("file://")


def _looks_like_branch(ref: str) -> bool:
    # tether's tags are `tether.<id>`; Icechunk and Iceberg snapshot ids are
    # opaque alphanumerics. Branches are `main` or `tether.ws.<dataset>.<bookmark>`.
    return ref == "main" or ref.startswith("tether.ws.")


def parse(key: str, address: str) -> Ref:
    version: int | None = None
    if m := _VERSION_SUFFIX.match(address):
        address, version = m.group("head"), int(m.group("version"))
    location, sep, ref_part = address.partition("#")
    return Ref(key=key, location=location, ref=ref_part if sep else None, version=version)


def local_refs(root: str | Path) -> dict[str, str]:
    """The DATA_REFS a laptop uses: every store under one local directory."""
    root = Path(root)
    return {
        ICECHUNK_KEY: f"{root / 'greetings.icechunk'}#main",
        ICEBERG_KEY: "lake.greetings_daily#main",
        LANCE_KEY: f"{root / 'greetings.lance'}#main",
        DELTA_KEY: str(root / "greetings_log.delta"),
        FILE_KEY: f"{root / 'raw' / 'uploads'}/",
    }


def local_iceberg_catalog(root: str | Path) -> dict[str, str]:
    """A pyiceberg SQL catalog on sqlite with a local warehouse: no services."""
    root = Path(root)
    return {
        "type": "sql",
        "uri": f"sqlite:///{root / 'iceberg-catalog.db'}",
        "warehouse": f"file://{root / 'iceberg-warehouse'}",
    }


def data_root() -> str:
    return os.environ.get("DATA_ROOT", ".data")


def refs() -> dict[str, str]:
    raw = os.environ.get("DATA_REFS")
    if raw:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("DATA_REFS must be a JSON object of key -> address")
        return {str(k): str(v) for k, v in parsed.items()}
    return local_refs(data_root())


def ref(key: str) -> Ref:
    try:
        return parse(key, refs()[key])
    except KeyError:
        raise KeyError(
            f"DATA_REFS has no entry for {key!r}; whichever provider built it "
            "(tofu stack, tether fork job, prod deployment) must list every "
            "object in the dataset's .tether/objects/"
        ) from None
