"""Hello-world webapp.

Runtime contract with the platform's workloads module:
  - listens on :8080 (webapp_container_port);
  - DATABASE_URL arrives via the module-managed secret (envFrom) — for a
    preview that is a copy-on-write Neon branch, for prod the real database;
  - /healthz is the readiness/liveness probe target and must not touch the DB
    (a database blip should not have the kubelet restarting pods);
  - APP_ENV is plain env (webapp_env) so the page can say where it runs.
"""

import os
from pathlib import Path

import marimo
from db.engine import get_engine
from db.models import Greeting
from fastapi import FastAPI, Request
from pydantic import BaseModel
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

app = FastAPI(title="lab-platform template app")

# Apps with built-in notebooks: marimo serves the notebook below as a reactive
# read-only page (run mode — visitors drive the UI elements, never the code).
# There's no auth here by design: the platform serves this app on the private
# tailnet, so reachability is the access control. Put it behind a login check
# before exposing it publicly.
_notebooks = (
    marimo.create_asgi_app()
    .with_app(path="", root=str(Path(__file__).parent / "notebooks" / "greetings.py"))
    .build()
)
app.mount("/notebooks", _notebooks)

_engine: Engine | None = None


def engine() -> Engine:
    """Lazy so importing the app (tests, /healthz) needs no DATABASE_URL."""
    global _engine
    if _engine is None:
        _engine = get_engine()
    return _engine


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/whoami")
def whoami(request: Request) -> dict:
    """Who the platform says is calling — identity for free on the tailnet.

    Served through the platform's private ingress (a Tailscale Ingress proxy),
    every request carries the caller's tailnet login in identity headers; and
    since the tailnet's login provider is your IdP (e.g. Google), that login is
    a real user identity. No OAuth client, no redirect URIs, no session state —
    which is exactly what per-PR preview URLs want. Build per-user features on
    these headers only where the proxy is the sole route to the pod; local dev
    and in-cluster calls simply see null.
    """
    return {
        "login": request.headers.get("Tailscale-User-Login"),
        "name": request.headers.get("Tailscale-User-Name"),
    }


@app.get("/")
def index() -> dict:
    with Session(engine()) as session:
        count = session.scalar(select(func.count()).select_from(Greeting))
    return {
        "message": "Hello, world",
        "environment": os.environ.get("APP_ENV", "dev"),
        "greetings": count,
    }


class GreetingIn(BaseModel):
    name: str


@app.post("/greetings", status_code=201)
def create_greeting(body: GreetingIn) -> dict:
    with Session(engine()) as session:
        greeting = Greeting(name=body.name)
        session.add(greeting)
        session.commit()
        return {"id": greeting.id, "name": greeting.name}
