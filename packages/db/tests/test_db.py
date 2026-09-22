from pathlib import Path

import pytest
from db.engine import database_url
from db.models import Base, Greeting
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import Session

ALEMBIC_INI = Path(__file__).parents[1] / "alembic.ini"


def test_database_url_rewrites_to_psycopg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db?sslmode=require")
    assert database_url().startswith("postgresql+psycopg://")


def test_database_url_leaves_other_schemes_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///tmp.db")
    assert database_url() == "sqlite:///tmp.db"


def test_purge_sessions_empties_the_table(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime

    from db import maintenance
    from db.models import Session as LoginSession
    from db.models import User

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/preview.db")
    engine = create_engine(f"sqlite:///{tmp_path}/preview.db")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = User(email="prod-user@lab.org")
        session.add(user)
        session.flush()
        session.add(
            LoginSession(
                id="hmac-of-a-prod-session", user_id=user.id, expires_at=datetime(2999, 1, 1)
            )
        )
        session.commit()

    assert maintenance.main(["purge-sessions"]) == 0
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(LoginSession)) == 0
        assert session.scalar(select(func.count()).select_from(User)) == 1  # users stay
    assert maintenance.main(["lock-notebook-roles"]) == 0  # SQLite has no roles
    assert maintenance.main(["nonsense"]) == 2


def test_greeting_roundtrip() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Greeting(name="world"))
        session.commit()
        count = session.scalar(select(func.count()).select_from(Greeting))

    assert count == 1


def test_migrations_round_trip_on_sqlite(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{tmp_path}/migrated.db"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(ALEMBIC_INI))
    command.upgrade(config, "head")
    engine = create_engine(url)
    tables = inspect(engine)
    assert {"users", "memberships", "sessions", "groups", "greetings"} <= set(
        tables.get_table_names()
    )
    columns = {c["name"]: c for c in tables.get_columns("memberships")}
    assert {"source", "group_id"} <= set(columns) and not columns["source"]["nullable"]
    assert [u["column_names"] for u in tables.get_unique_constraints("memberships")] == [
        ["user_id", "group_name", "source"]
    ]

    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]


def test_scoped_is_a_no_op_without_row_security() -> None:
    # SQLite has no roles: the query's own filter (Greeting.visible_to) is the rule.
    from db.engine import scoped

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([Greeting(name="public"), Greeting(name="grouped", group="/lab")])
        session.commit()
        assert len(scoped(session, ["/lab"]).scalars(select(Greeting)).all()) == 2


def test_scoped_refuses_group_names_it_cannot_encode() -> None:
    from db.engine import scoped

    with Session(create_engine("sqlite:///:memory:")) as session, pytest.raises(ValueError):
        scoped(session, ["/lab", "a,b"])
