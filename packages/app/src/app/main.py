"""Hello-world webapp.

Runtime contract with the platform's workloads module:
  - listens on :8080 (webapp_container_port);
  - DATABASE_URL arrives via the module-managed secret (envFrom) — for a
    preview that is a copy-on-write Neon branch, for prod the real database;
  - /healthz is the readiness/liveness probe target and must not touch the DB
    (a database blip should not have the kubelet restarting pods);
  - APP_ENV is plain env (webapp_env) so the page can say where it runs;
  - identity: AUTH_MODE names the one source the app accepts -- IDENTITY_HEADER
    ("headers": the private network's proxy names the caller), OIDC_* +
    SESSION_SECRET ("oidc": the app runs its own login against the platform's
    issuer), or the proxy's verified ID token (AUTH_PROXIED + IDENTITY_JWT_*).
    COOKIE_SECURE marks both of the app's cookies Secure. See app.auth.
    APP_ADMIN_GROUP / APP_ADMIN_EMAILS name the superadmins, who administer
    every app-managed group (app.groups).
"""

import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import dataset
import marimo
from db.engine import scoped
from db.models import Greeting
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import auth, groups, runtime
from app.auth import CurrentIdentity, Identity, require_user

app = FastAPI(title="lab-platform template app")

# Authlib keeps the OAuth state/nonce in Starlette's signed-cookie session for
# the few seconds of a login round trip. SESSION_SECRET comes from the
# workloads module in auth mode "oidc"; a random one is fine otherwise (the
# login session itself is a database row, app.auth, not this cookie).
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32),
    session_cookie="app_oauth_state",
    same_site="lax",
    # Behind the platform's ingress the app sees plain http, so Secure cannot
    # be inferred from the request; the module passes auth.cookie_secure.
    https_only=auth.settings().cookie_secure is True,
)
app.include_router(auth.router)
# The app's own groups (app:<name>), next to the identity provider's.
app.include_router(groups.router)

# Apps with built-in notebooks: marimo serves the notebook below as a reactive
# read-only page (run mode — visitors drive the UI elements, never the code).
# The page is subject to the same authorization as the API: it resolves the
# viewer from the page request (app.auth.identity_from) and reads through
# Greeting.visible_to (and, on PostgreSQL, row-level security: db.engine.scoped),
# so a group's greetings reach only its members here too.
_notebooks = (
    marimo.create_asgi_app()
    .with_app(path="", root=str(Path(__file__).parent / "notebooks" / "greetings.py"))
    .build()
)
app.mount("/notebooks", _notebooks)


def engine() -> Engine:
    """The shared lazy engine (app.runtime); kept here for the tests' sake."""
    return runtime.engine()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/whoami")
def whoami(identity: CurrentIdentity) -> dict:
    """Who the platform says is calling, and how it knows.

    `source` is "session" after an OIDC login through /login, "token" when the
    platform's oauth2-proxy forwarded a verified ID token, "header" when a
    trusted proxy (the tailnet's Ingress) named the caller, null when nobody
    did (local dev, in-cluster calls, AUTH_MODE=none). Either way the caller is
    now a row in users/memberships -- on THIS environment's database branch.
    """
    if identity is None:
        return {"login": None, "name": None, "groups": [], "source": None}
    return {
        "login": identity.login,
        "name": identity.user.name,
        "groups": identity.groups,
        "source": identity.source,
    }


@app.get("/data")
def data() -> dict:
    """Which state of which data object this deployment is wired to.

    The DATA_REFS contract (the ``dataset`` package) as the pods received it:
    in a preview, branches of the forked (or stamped) stores; in prod, ``main``
    at the real locations; on a laptop, paths under DATA_ROOT. Handy on a
    preview URL to confirm the PR is looking at its own fork, and a small
    example of the app knowing its dataset without touching a store.
    """
    return {
        key: {"location": r.location, "ref": r.ref, "pinned": r.pinned} for key, r in _data_refs()
    }


def _data_refs() -> list[tuple[str, dataset.Ref]]:
    return [(key, dataset.parse(key, address)) for key, address in sorted(dataset.refs().items())]


def _visible_to(identity: Identity | None):
    """Greetings the caller may see: public ones, plus their groups' ones."""
    return Greeting.visible_to(identity.groups if identity else [])


@contextmanager
def _as_caller(identity: Identity | None) -> Iterator[Session]:
    """A session the database itself confines to the caller's greetings.

    On PostgreSQL, row-level security (db.engine.scoped) enforces the same
    rule as `_visible_to`, which the queries keep applying: SQLite has no
    row security, and the database's check backs up the code's.
    """
    with Session(engine()) as session:
        yield scoped(session, identity.groups if identity else [])


@app.get("/")
def index(identity: CurrentIdentity) -> dict:
    with _as_caller(identity) as session:
        count = session.scalar(
            select(func.count()).select_from(Greeting).where(_visible_to(identity))
        )
    return {
        "message": "Hello, world",
        "environment": os.environ.get("APP_ENV", "dev"),
        "greetings": count,
        "login": identity.login if identity else None,
    }


class GreetingIn(BaseModel):
    name: str
    # A group makes the greeting visible to that group only; the caller must
    # belong to it. None = public.
    group: str | None = None


def _greeting_out(g: Greeting) -> dict:
    return {"id": g.id, "name": g.name, "group": g.group, "owner_id": g.owner_id}


@app.get("/greetings")
def list_greetings(identity: CurrentIdentity) -> list[dict]:
    """Different groups, different data: each caller sees public greetings
    plus those of the groups they belong to."""
    with _as_caller(identity) as session:
        rows = session.scalars(
            select(Greeting).where(_visible_to(identity)).order_by(Greeting.id)
        ).all()
        return [_greeting_out(g) for g in rows]


@app.get("/greetings/mine")
def my_greetings(identity: Annotated[Identity, Depends(require_user)]) -> list[dict]:
    with _as_caller(identity) as session:
        rows = session.scalars(
            select(Greeting).where(Greeting.owner_id == identity.user.id).order_by(Greeting.id)
        ).all()
        return [_greeting_out(g) for g in rows]


@app.post("/greetings", status_code=201)
def create_greeting(body: GreetingIn, identity: CurrentIdentity) -> dict:
    if body.group is not None:
        if identity is None:
            raise HTTPException(status_code=401, detail="login required to post to a group")
        if body.group not in identity.groups:
            raise HTTPException(status_code=403, detail=f"not a member of {body.group!r}")
    with _as_caller(identity) as session:
        greeting = Greeting(
            name=body.name,
            group=body.group,
            owner_id=identity.user.id if identity else None,
        )
        session.add(greeting)
        # Read the row back inside the scoped transaction: the commit ends it.
        session.flush()
        out = _greeting_out(greeting)
        session.commit()
    return out
