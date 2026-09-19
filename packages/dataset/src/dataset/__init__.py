"""The app's dataset, as code.

``packages/dataset`` is the tether dataset root (``tether.toml`` +
``.tether/objects/``, one manifest per data object) and the Python package
that lets the rest of the workspace use it:

- :mod:`dataset.refs` -- the DATA_REFS contract: the keys, how an address is
  parsed, and the local defaults a laptop falls back to. No dependencies.
- :mod:`dataset.openers` -- one native opener per store kind (Icechunk,
  Iceberg, Lance, Delta, object-store prefix); needs ``dataset[stores]``.

Keeping the manifests and the code that names them in one package is what
makes the key list checkable (``tests/test_manifests.py``) and the dataset
shippable: another workspace, or another repo, depends on ``dataset`` the way
``workflows`` does. A dataset that outgrows one app moves to its own
repository and comes back as a git submodule -- README "Where the dataset
lives".
"""

from dataset.refs import (
    DELTA_KEY,
    FILE_KEY,
    ICEBERG_KEY,
    ICECHUNK_KEY,
    KEYS,
    LANCE_KEY,
    NEON_KEYS,
    SERVICE_DB_KEYS,
    Ref,
    local_iceberg_catalog,
    local_refs,
    parse,
    ref,
    refs,
)

__all__ = [
    "DELTA_KEY",
    "FILE_KEY",
    "ICEBERG_KEY",
    "ICECHUNK_KEY",
    "KEYS",
    "LANCE_KEY",
    "NEON_KEYS",
    "SERVICE_DB_KEYS",
    "Ref",
    "local_iceberg_catalog",
    "local_refs",
    "parse",
    "ref",
    "refs",
]
