import pytest
from db.engine import database_url
from db.models import Base, Greeting
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session


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
    assert maintenance.main(["nonsense"]) == 2


def test_greeting_roundtrip() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Greeting(name="world"))
        session.commit()
        count = session.scalar(select(func.count()).select_from(Greeting))

    assert count == 1
