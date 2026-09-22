"""lock-notebook-roles against a real PostgreSQL (TEST_POSTGRES_URL, as in test_rls.py).

A throwaway database owned by a CREATEROLE login role that made the notebook
roles itself, as the project owner does on Neon.
"""

import os
import secrets
from collections.abc import Iterator

import pytest
from db import maintenance
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

SERVER = os.environ.get("TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not SERVER, reason="needs TEST_POSTGRES_URL (a PostgreSQL server)")


def can_login(admin: Engine, role: str) -> bool:
    with admin.connect() as c:
        return bool(
            c.scalar(text("SELECT rolcanlogin FROM pg_roles WHERE rolname = :r"), {"r": role})
        )


@pytest.fixture
def branch(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Engine, list[str]]]:
    server = make_url(SERVER)
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    suffix = secrets.token_hex(4)
    name, owner, password = f"branch_{suffix}", f"branch_owner_{suffix}", secrets.token_hex(16)
    roles = [f"nb_t{suffix}__authors", f"nb_t{suffix}__research"]
    with admin.connect() as c:
        c.execute(text(f"CREATE ROLE {owner} LOGIN CREATEROLE PASSWORD '{password}'"))
        c.execute(text(f"CREATE DATABASE {name} OWNER {owner}"))
    url = server.set(username=owner, password=password, database=name)
    as_owner = create_engine(url, isolation_level="AUTOCOMMIT")
    with as_owner.connect() as c:
        for role in roles:
            c.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
    as_owner.dispose()
    monkeypatch.setenv("DATABASE_URL", url.render_as_string(hide_password=False))
    try:
        yield admin, roles
    finally:
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            for role in roles:
                c.execute(text(f"DROP ROLE IF EXISTS {role}"))
            c.execute(text(f"DROP ROLE IF EXISTS {owner}"))
        admin.dispose()


def test_refuses_outside_neon(branch) -> None:
    # A plain PostgreSQL server: roles are server-wide, so locking would reach
    # every database on it.
    admin, roles = branch
    with admin.connect() as c:
        if c.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'neon_superuser'")):
            pytest.skip("this server has a neon_superuser role")
    assert maintenance.main(["lock-notebook-roles"]) == 1
    assert all(can_login(admin, role) for role in roles)


def test_locks_the_branchs_notebook_roles(branch) -> None:
    admin, roles = branch
    with admin.connect() as c:
        created = not c.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'neon_superuser'"))
        if created:
            c.execute(text("CREATE ROLE neon_superuser NOLOGIN"))
    try:
        assert maintenance.main(["lock-notebook-roles"]) == 0
        assert not any(can_login(admin, role) for role in roles)
        assert maintenance.main(["lock-notebook-roles"]) == 0  # nothing left to lock
    finally:
        if created:
            with admin.connect() as c:
                c.execute(text("DROP ROLE neon_superuser"))
