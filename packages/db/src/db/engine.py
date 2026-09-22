import os
from collections.abc import Iterable

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

# The role row-level security confines to what `app.groups` may see
# (migration 0004); the app's login role is a member.
READER_ROLE = "app_reader"


def database_url() -> str:
    """The DATABASE_URL the platform injects (workloads module -> secret envFrom).

    Neon/compose hand out URLs in several spellings; normalise the plain
    ``postgresql://`` scheme onto the psycopg (v3) driver this package ships so
    SQLAlchemy does not go looking for psycopg2.
    """
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def get_engine(**kwargs) -> Engine:
    kwargs.setdefault("pool_pre_ping", True)
    return create_engine(database_url(), **kwargs)


def scoped(session: Session, groups: Iterable[str]) -> Session:
    """Run the rest of `session`'s transaction as a viewer who is in `groups`.

    On PostgreSQL: ``SET LOCAL ROLE app_reader`` and ``app.groups`` =
    `groups`, so row-level security lets the queries see (and write) public
    greetings and those groups' only. Both last until the transaction ends --
    after a commit or rollback, call it again. Elsewhere (SQLite) it does
    nothing, and the query's own filter (`Greeting.visible_to`, which every
    reader applies anyway) is the only rule.

    `app.groups` is a comma-joined list, so a group name with a comma is
    refused rather than split into two (IdP paths and ``app:`` names have none).
    """
    groups = sorted({g for g in groups if g})
    if any("," in g for g in groups):
        raise ValueError(f"group names cannot contain ',': {groups!r}")
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text(f"SET LOCAL ROLE {READER_ROLE}"))
        session.execute(
            text("SELECT set_config('app.groups', :groups, true)"), {"groups": ",".join(groups)}
        )
    return session
