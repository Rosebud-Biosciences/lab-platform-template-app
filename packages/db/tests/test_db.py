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


def test_greeting_roundtrip() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Greeting(name="world"))
        session.commit()
        count = session.scalar(select(func.count()).select_from(Greeting))

    assert count == 1
