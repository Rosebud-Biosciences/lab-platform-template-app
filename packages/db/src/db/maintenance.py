"""Housekeeping a stamped environment runs against its own database.

    python -m db.maintenance purge-sessions

A preview's database is a Neon branch (tofu's, or a tether fork) of prod's,
so it starts with prod's rows. preview-up runs this right after the
migrations. Never point it at prod.

- purge-sessions: prod's login sessions are stored as HMAC(SESSION_SECRET,
  id) with prod's secret, so they cannot log anyone in to the preview or,
  replayed, to prod; purging them anyway keeps a preview's database free of
  prod's live-session metadata.

Prod's roles reach the branch too, the platform's nb_<tenant>__<group>
notebook roles among them; Neon gives them new passwords there, because the
branch is made from a protected branch (infra/preview checks that it is).
"""

import sys

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession

from db.engine import get_engine
from db.models import Session


def purge_sessions() -> int:
    """Delete every login session; returns how many there were."""
    with DbSession(get_engine()) as db:
        count = db.scalar(select(func.count()).select_from(Session)) or 0
        db.execute(delete(Session))
        db.commit()
    return count


def main(argv: list[str]) -> int:
    if argv == ["purge-sessions"]:
        print(f"purged {purge_sessions()} session(s)")
        return 0
    print("usage: python -m db.maintenance purge-sessions", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
