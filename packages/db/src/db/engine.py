import os

from sqlalchemy import Engine, create_engine


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
