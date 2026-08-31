from pathlib import Path

import dagster as dg
import pytest
from db.engine import get_engine
from db.models import Base, Greeting
from sqlalchemy.orm import Session
from workflows.definitions import (
    GreetingBatch,
    defs,
    greeting_stream_sensor,
    greetings_digest,
    ray_fanned_greetings,
    scheduled_greeting,
)


def test_definitions_load() -> None:
    dg.Definitions.validate_loadable(defs)


def test_greeting_asset_writes_a_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/wf.db")
    Base.metadata.create_all(get_engine())

    result = dg.materialize([scheduled_greeting])

    assert result.success
    materialization = result.asset_materializations_for_node("scheduled_greeting")[0]
    assert materialization.metadata["total_greetings"].value == 1


def test_stream_sensor_batches_new_rows_then_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/stream.db")
    engine = get_engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(Greeting(name=f"row-{i}") for i in range(3))
        session.commit()

    ctx = dg.build_sensor_context(cursor=None)
    request = greeting_stream_sensor(ctx)
    assert isinstance(request, dg.RunRequest)
    assert request.run_key == "greetings-0-3"
    assert ctx.cursor == "3"

    # Nothing new since the cursor advanced -> the next tick skips.
    again = greeting_stream_sensor(dg.build_sensor_context(cursor=ctx.cursor))
    assert isinstance(again, dg.SkipReason)


def test_digest_reads_only_its_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/digest.db")
    engine = get_engine()
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(Greeting(name=f"row-{i}") for i in range(5))
        session.commit()

    result = dg.materialize(
        [greetings_digest],
        run_config=dg.RunConfig(ops={"greetings_digest": GreetingBatch(after_id=2, through_id=5)}),
    )

    assert result.success
    materialization = result.asset_materializations_for_node("greetings_digest")[0]
    assert materialization.metadata["batch_size"].value == 3
    assert materialization.metadata["id_window"].value == "(2, 5]"


def test_ray_fanout_writes_all_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/ray.db")
    monkeypatch.delenv("RAY_ADDRESS", raising=False)  # local Ray, no cluster
    Base.metadata.create_all(get_engine())

    result = dg.materialize([ray_fanned_greetings])

    assert result.success
    materialization = result.asset_materializations_for_node("ray_fanned_greetings")[0]
    assert materialization.metadata["fanned_out"].value == 8
    assert materialization.metadata["total_greetings"].value == 8
