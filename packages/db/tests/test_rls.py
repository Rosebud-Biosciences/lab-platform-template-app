"""Row-level security on greetings (migration 0004), against a real PostgreSQL.

Skipped unless TEST_POSTGRES_URL names a server whose user is a superuser
(it creates a database and roles): CI's postgres service, or locally

    docker run -d --rm --name rls-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:17
    TEST_POSTGRES_URL=postgresql+psycopg://postgres:postgres@localhost:55432/postgres uv run pytest

The module migrates a throwaway database owned, as on Neon, by a login role
that is not a superuser (superusers bypass row security, so they would prove
nothing about the owner), and drops both afterwards.
"""

import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from db.engine import scoped
from db.models import Greeting
from sqlalchemy import Connection, Engine, create_engine, insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

SERVER = os.environ.get("TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not SERVER, reason="needs TEST_POSTGRES_URL (a PostgreSQL server)")

ALEMBIC_INI = Path(__file__).parents[1] / "alembic.ini"
# The platform's login role for group /lab/authors' notebooks.
NOTEBOOK = "nb_lab__authors"
SEED = {"hello": None, "for authors": "/lab/authors", "for research": "/acme/research"}
EVERYTHING = list(SEED)


@dataclass
class Database:
    owner: Engine  # owns the tables and runs the migrations; the app logs in as it
    notebook: Engine  # nb_lab__authors, connecting directly


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    server = make_url(SERVER)
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    suffix = secrets.token_hex(4)
    name, owner, password = f"rls_{suffix}", f"rls_owner_{suffix}", secrets.token_hex(16)
    with admin.connect() as c:
        c.execute(text(f"CREATE ROLE {owner} LOGIN CREATEROLE PASSWORD '{password}'"))
        # The roles are cluster-wide: where an earlier database (or test run)
        # created them, a cluster admin hands them to the new owner.
        for role in ("app_reader", "app_notebook"):
            if c.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}):
                c.execute(text(f"GRANT {role} TO {owner} WITH ADMIN OPTION"))
        c.execute(text(f"CREATE DATABASE {name} OWNER {owner}"))
        c.execute(text(f"DROP ROLE IF EXISTS {NOTEBOOK}"))
        c.execute(text(f"CREATE ROLE {NOTEBOOK} LOGIN PASSWORD '{password}'"))
    url = server.set(username=owner, password=password, database=name)
    engines: list[Engine] = []
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
            config = Config(str(ALEMBIC_INI))
            command.upgrade(config, "head")
            command.downgrade(config, "base")  # every downgrade runs on PostgreSQL too
            command.upgrade(config, "head")
        with admin.connect() as c:
            # What the platform does for each notebook role (modules/postgres-
            # group-roles): a plain grant, which on PostgreSQL 16+ also lets
            # the role SET ROLE app_notebook -- the policy must not care.
            c.execute(text(f"GRANT app_notebook TO {NOTEBOOK}"))
        engines = [create_engine(url), create_engine(url.set(username=NOTEBOOK))]
        with Session(engines[0]) as session:
            session.add_all(Greeting(name=n, group=g) for n, g in SEED.items())
            session.commit()
        yield Database(owner=engines[0], notebook=engines[1])
    finally:
        for engine in engines:
            engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            c.execute(text(f"DROP ROLE IF EXISTS {NOTEBOOK}"))
            c.execute(text(f"DROP ROLE IF EXISTS {owner}"))
        admin.dispose()


def names(db: Session | Connection) -> list[str]:
    """Every greeting the database lets this reader see: no filter of our own."""
    return list(db.scalars(select(Greeting.name).order_by(Greeting.id)))


def test_the_owner_is_unaffected(database: Database) -> None:
    with Session(database.owner) as session:
        assert names(session) == EVERYTHING


def test_the_app_reader_without_groups_sees_public_rows(database: Database) -> None:
    with Session(database.owner) as session:
        assert names(scoped(session, [])) == ["hello"]


def test_the_app_reader_sees_the_groups_it_names(database: Database) -> None:
    with Session(database.owner) as session:
        assert names(scoped(session, ["/lab/authors"])) == ["hello", "for authors"]
    with Session(database.owner) as session:
        assert names(scoped(session, ["/lab/authors", "/acme/research"])) == EVERYTHING


def test_the_scope_ends_with_the_transaction(database: Database) -> None:
    with Session(database.owner) as session:
        assert names(scoped(session, [])) == ["hello"]
        session.commit()
        assert names(session) == EVERYTHING  # the owner again; nothing leaks into the pool
        assert session.scalar(text("SELECT current_setting('app.groups', true)")) in (None, "")


def test_the_app_reader_cannot_write_to_a_foreign_group(database: Database) -> None:
    with Session(database.owner) as session:
        scoped(session, ["/lab/authors"])
        session.add_all([Greeting(name="public"), Greeting(name="ours", group="/lab/authors")])
        session.flush()
        session.add(Greeting(name="theirs", group="/acme/research"))
        with pytest.raises(DBAPIError, match="row-level security"):
            session.flush()


def test_a_notebook_role_sees_the_group_its_name_spells(database: Database) -> None:
    with database.notebook.connect() as c:
        assert c.scalar(text("SELECT app_viewer_groups()")) == ["/lab/authors"]
        assert names(c) == ["hello", "for authors"]

        # It cannot widen its view by naming other groups ...
        c.execute(text("SET app.groups = '/acme/research'"))
        assert names(c) == ["hello", "for authors"]

        # ... nor by becoming app_notebook and then naming them: the group
        # comes from session_user, which SET ROLE does not change.
        c.execute(text("SET ROLE app_notebook"))
        c.execute(text("SET app.groups = '/acme/research,/lab/pipelines'"))
        assert c.scalar(text("SELECT app_viewer_groups()")) == ["/lab/authors"]
        assert names(c) == ["hello", "for authors"]


def test_a_notebook_role_only_reads(database: Database) -> None:
    with (
        database.notebook.connect() as c,
        pytest.raises(DBAPIError, match="permission denied"),
    ):
        c.execute(insert(Greeting).values(name="from a notebook", group="/lab/authors"))
