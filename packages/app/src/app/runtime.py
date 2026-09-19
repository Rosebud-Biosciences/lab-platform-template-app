"""Process-wide runtime state: the lazily created database engine.

Lazy so importing the app (tests, /healthz) needs no DATABASE_URL, and in one
place so the routes in main.py and the login flow in auth.py share it.
"""

from db.engine import get_engine
from sqlalchemy import Engine

_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = get_engine()
    return _engine


def reset_engine() -> None:
    """Forget the engine (tests point DATABASE_URL somewhere new per test)."""
    global _engine
    _engine = None
