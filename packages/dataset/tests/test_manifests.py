"""The three places that name the data objects must agree.

- the tether manifests (``.tether/objects/<key>.toml``): what tether forks,
  pins and verifies -- the source of truth;
- ``dataset.refs``: the keys the code asks DATA_REFS for;
- ``infra/preview/data.tf``: the addresses the tofu-mode stack builds.

Adding a store is `tether add`, one constant, one asset, one line of HCL, and
this test is what notices when one of the four is forgotten.
"""

import re
import tomllib
from pathlib import Path

from dataset import KEYS, NEON_KEYS

DATASET_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = DATASET_ROOT.parents[1]


def manifest_keys() -> dict[str, str]:
    """key -> kind for every manifest."""
    out = {}
    for path in (DATASET_ROOT / ".tether" / "objects").rglob("*.toml"):
        doc = tomllib.loads(path.read_text())
        out[doc["key"]] = doc["kind"]
    return out


def test_manifests_and_code_name_the_same_objects() -> None:
    manifests = manifest_keys()
    neon = {k for k, kind in manifests.items() if kind == "neon"}
    others = set(manifests) - neon
    assert neon == set(NEON_KEYS), "Postgres objects travel as DATABASE_URL; keep NEON_KEYS in step"
    assert others == set(KEYS), "every non-database manifest needs a key constant (and vice versa)"


def test_tofu_stack_builds_every_key() -> None:
    hcl = (REPO_ROOT / "infra" / "preview" / "data.tf").read_text()
    quoted = set(re.findall(r'^\s*"([a-z]+/[a-z_]+)"\s*=', hcl, re.M))
    assert quoted == set(KEYS), "infra/preview/data.tf's tofu_data_refs must list exactly the keys"


def test_dataset_config_forks_eagerly() -> None:
    config = tomllib.loads((DATASET_ROOT / "tether.toml").read_text())
    assert config["new"]["fork"] == "eager", (
        "CI hands pods branch names before their first write; forks must exist by then"
    )
