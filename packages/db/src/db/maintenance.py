"""Housekeeping a stamped environment runs against its own database.

    python -m db.maintenance purge-sessions
    python -m db.maintenance lock-notebook-roles

A preview's database is a Neon branch (tofu's, or a tether fork) of prod's,
so it starts with prod's rows and prod's roles. preview-up runs both right
after the migrations. Never point them at prod.

- purge-sessions: prod's login sessions are stored as HMAC(SESSION_SECRET,
  id) with prod's secret, so they cannot log anyone in to the preview or,
  replayed, to prod; purging them anyway keeps a preview's database free of
  prod's live-session metadata.
- lock-notebook-roles: Neon copies roles created by SQL -- the platform's
  nb_<tenant>__<group> notebook roles -- to a branch with their passwords,
  so prod's notebook credentials would log in to the branch, whose row
  security a PR's migrations may have changed. Previews run no notebooks:
  the branch's copies lose their login.
"""

import sys

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session as DbSession

from db.engine import get_engine
from db.models import Session

NOTEBOOK_ROLES = r"nb\_%"


class NotABranch(Exception):
    """The database is not on Neon, so its roles are not the branch's own."""


def purge_sessions() -> int:
    """Delete every login session; returns how many there were."""
    with DbSession(get_engine()) as db:
        count = db.scalar(select(func.count()).select_from(Session)) or 0
        db.execute(delete(Session))
        db.commit()
    return count


def lock_notebook_roles() -> list[str]:
    """Turn off login for every nb_* role; returns the roles changed.

    Only on Neon, where each branch has roles of its own: on any other
    PostgreSQL server roles are server-wide, and this would lock them for
    every database on it, prod's included. SQLite has no roles.
    """
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        return []
    with engine.begin() as conn:
        if not conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'neon_superuser'")):
            raise NotABranch("not a Neon database: its roles are server-wide")
        roles = list(
            conn.scalars(
                text(
                    "SELECT rolname FROM pg_roles WHERE rolname LIKE :pattern AND rolcanlogin"
                    " ORDER BY rolname"
                ),
                {"pattern": NOTEBOOK_ROLES},
            )
        )
        quote = conn.dialect.identifier_preparer.quote
        for role in roles:
            conn.execute(text(f"ALTER ROLE {quote(role)} NOLOGIN"))
    return roles


def main(argv: list[str]) -> int:
    if argv == ["purge-sessions"]:
        print(f"purged {purge_sessions()} session(s)")
        return 0
    if argv == ["lock-notebook-roles"]:
        try:
            roles = lock_notebook_roles()
        except NotABranch as e:
            print(f"refusing to lock notebook roles: {e}", file=sys.stderr)
            return 1
        print(f"locked {len(roles)} notebook role(s): {', '.join(roles) or '-'}")
        return 0
    print("usage: python -m db.maintenance purge-sessions | lock-notebook-roles", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
