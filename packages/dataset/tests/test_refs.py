import json

import pytest
from dataset import ICECHUNK_KEY, KEYS, local_refs, parse, ref, refs


def test_parse_addresses() -> None:
    r = parse("k", "s3://b/x.icechunk#tether.ws.abcd1234.pr7")
    assert (r.location, r.branch, r.pinned) == (
        "s3://b/x.icechunk",
        "tether.ws.abcd1234.pr7",
        False,
    )
    assert not r.is_local

    r = parse("k", "s3://b/x.icechunk#tether.abcd1234.deadbeef")
    assert r.pinned and r.ref == "tether.abcd1234.deadbeef"

    r = parse("k", "s3://b/x.delta@v3")
    assert (r.location, r.version, r.pinned) == ("s3://b/x.delta", 3, True)

    r = parse("k", "s3://b/x.lance#main@v2")
    assert (r.branch, r.version) == ("main", 2)

    r = parse("k", "lake.t")
    assert (r.branch, r.pinned) == ("main", False)

    r = parse("k", "file:///tmp/x.lance#main")
    assert r.is_local and r.local_path == "/tmp/x.lance"


def test_local_defaults_cover_every_key(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATA_REFS", raising=False)
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    assert set(refs()) == set(KEYS) == set(local_refs(tmp_path))
    assert ref(ICECHUNK_KEY).is_local


def test_missing_key_names_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_REFS", json.dumps({"other": "x"}))
    with pytest.raises(KeyError, match="DATA_REFS has no entry"):
        ref(ICECHUNK_KEY)


def test_data_refs_must_be_an_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_REFS", "[]")
    with pytest.raises(ValueError, match="JSON object"):
        refs()
