import os
import secrets
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from app import auth, main, runtime
from db.models import Base, Greeting, Membership, Session, User
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import ECKey, KeySet, RSAKey
from sqlalchemy import create_engine, func, select, text, true
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session as DbSession

ISSUER = "http://dex.dex.svc.cluster.local:5556/dex"
ALEMBIC_INI = Path(__file__).parents[2] / "db" / "alembic.ini"

AUTH_ENV = (
    "AUTH_MODE",
    "AUTH_PROXIED",
    "OIDC_ISSUER_URL",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "OIDC_REDIRECT_URL",
    "IDENTITY_HEADER",
    "IDENTITY_GROUPS_HEADER",
    "IDENTITY_NAME_HEADER",
    "IDENTITY_JWT_ISSUER",
    "IDENTITY_JWT_AUDIENCE",
    "COOKIE_SECURE",
    "APP_ADMIN_GROUP",
    "APP_ADMIN_EMAILS",
)


def _fresh_app(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_SECRET", "test-environment-secret")
    for var in AUTH_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(auth, "_oauth", None)
    runtime.reset_engine()  # fresh engine per test


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _fresh_app(monkeypatch, f"sqlite:///{tmp_path}/app.db")
    Base.metadata.create_all(runtime.engine())
    return TestClient(main.app)


def test_healthz_needs_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    runtime.reset_engine()
    assert TestClient(main.app).get("/healthz").json() == {"status": "ok"}


def test_index_counts_greetings(client: TestClient) -> None:
    body = client.get("/").json()
    assert body["message"] == "Hello, world"
    assert body["greetings"] == 0
    assert body["login"] is None


def test_post_greeting_increments_count(client: TestClient) -> None:
    created = client.post("/greetings", json={"name": "ci"})
    assert created.status_code == 201
    assert created.json()["name"] == "ci"
    assert created.json()["group"] is None

    assert client.get("/").json()["greetings"] == 1


def test_notebooks_are_mounted(client: TestClient) -> None:
    # The marimo mount serves the notebook's HTML shell (the kernel itself
    # only spins up when a browser opens a session).
    response = client.get("/notebooks/", follow_redirects=True)
    assert response.status_code == 200
    assert "marimo" in response.text.lower()


# ------------------------------------------------------------------------------
# Which identity source the app accepts (AUTH_MODE)
# ------------------------------------------------------------------------------


def test_whoami_is_anonymous_without_identity(client: TestClient) -> None:
    # Off the tailnet (local dev, in-cluster) there is simply no identity.
    assert client.get("/whoami").json() == {
        "login": None,
        "name": None,
        "groups": [],
        "source": None,
    }


def test_no_header_is_trusted_unless_named(client: TestClient, monkeypatch) -> None:
    # The module sets OIDC_* and no IDENTITY_HEADER in "oidc" mode: a request
    # that bypassed the proxy must not be able to claim an identity.
    monkeypatch.setenv("OIDC_ISSUER_URL", ISSUER)
    monkeypatch.setenv("OIDC_CLIENT_ID", "webapp")
    spoofed = {"Tailscale-User-Login": "admin@lab.org", "X-Forwarded-Email": "admin@lab.org"}
    assert client.get("/whoami", headers=spoofed).json()["source"] is None
    with DbSession(runtime.engine()) as db:
        assert db.scalar(select(User)) is None


def test_oidc_mode_ignores_a_named_header(client: TestClient, monkeypatch) -> None:
    # Even if IDENTITY_HEADER leaks into the env, "oidc" mode trusts only the
    # app's own login (or a verified token when proxied).
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("IDENTITY_HEADER", "Tailscale-User-Login")
    assert (
        client.get("/whoami", headers={"Tailscale-User-Login": "admin@lab.org"}).json()["source"]
        is None
    )


def test_none_mode_identifies_nobody(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "none")
    monkeypatch.setenv("IDENTITY_HEADER", "Tailscale-User-Login")
    monkeypatch.setenv("OIDC_ISSUER_URL", ISSUER)
    monkeypatch.setenv("OIDC_CLIENT_ID", "webapp")
    assert client.get("/whoami", headers={"Tailscale-User-Login": "a@b"}).json()["source"] is None
    assert client.get("/login", follow_redirects=False).status_code == 501


# ------------------------------------------------------------------------------
# Identity headers (auth mode "headers": the tailnet's proxy names the caller)
# ------------------------------------------------------------------------------


def test_whoami_trusts_the_identity_header_and_records_the_user(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # auth mode "headers": the module names the header the tailnet's proxy
    # sets, and the proxy asserts the caller's login (and display name) on
    # every request ...
    monkeypatch.setenv("AUTH_MODE", "headers")
    monkeypatch.setenv("IDENTITY_HEADER", "Tailscale-User-Login")
    body = client.get(
        "/whoami",
        headers={"Tailscale-User-Login": "alice@lab.org", "Tailscale-User-Name": "Alice"},
    ).json()
    assert body == {"login": "alice@lab.org", "name": "Alice", "groups": [], "source": "header"}

    # ... and the first sighting becomes a row in THIS database (a preview's
    # branch), like an OIDC login would.
    with DbSession(runtime.engine()) as db:
        user = db.scalar(select(User).where(User.email == "alice@lab.org"))
        assert user is not None and user.subject is None and user.name == "Alice"


def test_identity_header_name_is_configurable(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("IDENTITY_HEADER", "X-Forwarded-Email")
    monkeypatch.setenv("IDENTITY_GROUPS_HEADER", "X-Forwarded-Groups")
    body = client.get(
        "/whoami",
        headers={"X-Forwarded-Email": "bob@lab.org", "X-Forwarded-Groups": "pipelines, lab"},
    ).json()
    assert body["login"] == "bob@lab.org"
    assert body["groups"] == ["lab", "pipelines"]
    assert body["source"] == "header"
    # The Tailscale header is no longer trusted once another one is named.
    assert client.get("/whoami", headers={"Tailscale-User-Login": "x@y"}).json()["login"] is None


def test_header_requests_do_not_write_on_every_request(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("IDENTITY_HEADER", "Tailscale-User-Login")
    headers = {"Tailscale-User-Login": "ann@lab.org"}
    client.get("/whoami", headers=headers)
    with DbSession(runtime.engine()) as db:
        first = db.scalar(select(User.last_login_at).where(User.email == "ann@lab.org"))
    client.get("/whoami", headers=headers)
    with DbSession(runtime.engine()) as db:
        again = db.scalar(select(User.last_login_at).where(User.email == "ann@lab.org"))
    assert first is not None and again == first


def test_concurrent_first_sightings_create_one_user(client: TestClient) -> None:
    auth._create_by_email("race@lab.org")
    auth._create_by_email("race@lab.org")  # the losing insert must not raise
    with DbSession(runtime.engine()) as db:
        count = db.scalar(
            select(func.count()).select_from(User).where(User.email == "race@lab.org")
        )
    assert count == 1


# ------------------------------------------------------------------------------
# OIDC login (auth mode "oidc"): the issuer is stood in for by a fake token
# exchange; everything after it -- users, memberships, the session row and
# cookie -- is real and lives in the app's database.
# ------------------------------------------------------------------------------

KILGORE = {
    "sub": "CgRtb2Nr",
    "email": "kilgore@kilgore.trout",
    "email_verified": True,
    "name": "Kilgore Trout",
    "groups": ["authors", "lab"],
}


def claims(**overrides):
    async def exchange(request):
        return {**KILGORE, **overrides}

    return exchange


@pytest.fixture
def oidc(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OIDC_ISSUER_URL", ISSUER)
    monkeypatch.setenv("OIDC_CLIENT_ID", "webapp")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("OIDC_REDIRECT_URL", "http://testserver/auth/callback")
    monkeypatch.setattr(auth, "claims_from_callback", claims())
    return client


def login(client: TestClient) -> None:
    assert client.get("/auth/callback?code=x&state=y", follow_redirects=False).status_code == 303


def test_login_is_501_until_oidc_is_configured(client: TestClient) -> None:
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 501


class FakeIssuer:
    """Stands in for Authlib's client: the redirect to the issuer's /auth."""

    @staticmethod
    async def authorize_redirect(request, redirect_uri=None, **kwargs):
        from fastapi.responses import RedirectResponse

        return RedirectResponse(f"{ISSUER}/auth?redirect_uri={redirect_uri}", status_code=302)


def test_login_redirects_to_the_issuer(oidc: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_client", lambda: FakeIssuer())
    response = oidc.get("/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].startswith(f"{ISSUER}/auth")
    assert "redirect_uri=http://testserver/auth/callback" in response.headers["location"]


def test_login_uses_pkce(oidc: TestClient) -> None:
    assert auth._client().client_kwargs["code_challenge_method"] == "S256"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("/greetings", "/greetings"),
        ("//evil.example/steal", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example/", "/"),
        ("greetings", "/"),
        ("", "/"),
    ],
)
def test_safe_next(target: str, expected: str) -> None:
    assert auth.safe_next(target) == expected


def test_login_does_not_redirect_off_site(oidc: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(auth, "_client", lambda: FakeIssuer())
    oidc.get("/login?next=//evil.example/steal", follow_redirects=False)
    response = oidc.get("/auth/callback?code=x&state=y", follow_redirects=False)
    assert response.headers["location"] == "/"

    oidc.get("/login?next=/greetings", follow_redirects=False)
    response = oidc.get("/auth/callback?code=x&state=y", follow_redirects=False)
    assert response.headers["location"] == "/greetings"


def test_callback_creates_user_memberships_and_session(oidc: TestClient) -> None:
    response = oidc.get("/auth/callback?code=x&state=y", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert auth.SESSION_COOKIE in response.cookies

    with DbSession(runtime.engine()) as db:
        user = db.scalar(select(User).where(User.email == "kilgore@kilgore.trout"))
        assert user is not None
        assert user.subject == f"{ISSUER}|CgRtb2Nr"
        assert sorted(m.group for m in db.scalars(select(Membership))) == ["authors", "lab"]
        session_row = db.scalar(select(Session))
        assert session_row is not None and session_row.user_id == user.id

    # The cookie is the login: whoami now answers from the session row.
    body = oidc.get("/whoami").json()
    assert body == {
        "login": "kilgore@kilgore.trout",
        "name": "Kilgore Trout",
        "groups": ["authors", "lab"],
        "source": "session",
    }


def test_sessions_are_stored_hashed_with_the_environments_secret(
    oidc: TestClient, monkeypatch
) -> None:
    response = oidc.get("/auth/callback?code=x&state=y", follow_redirects=False)
    sid = response.cookies[auth.SESSION_COOKIE]
    with DbSession(runtime.engine()) as db:
        row_id = db.scalar(select(Session.id))
    assert row_id != sid and row_id == auth.session_row_id(sid)

    # A branched copy of this row is useless where SESSION_SECRET differs (a
    # preview holding prod's rows): the same cookie no longer resolves.
    monkeypatch.setenv("SESSION_SECRET", "another-environment")
    assert oidc.get("/whoami").json()["source"] is None


def test_callback_requires_a_verified_email(oidc: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(auth, "claims_from_callback", claims(email_verified=False))
    assert oidc.get("/auth/callback?code=x&state=y", follow_redirects=False).status_code == 403
    monkeypatch.setattr(auth, "claims_from_callback", claims(email_verified=None))
    assert oidc.get("/auth/callback?code=x&state=y", follow_redirects=False).status_code == 403


def test_users_are_keyed_by_subject(oidc: TestClient, monkeypatch) -> None:
    login(oidc)
    # Same subject, new email at the IdP: the same account, email updated.
    monkeypatch.setattr(auth, "claims_from_callback", claims(email="kt@trout.example"))
    login(oidc)
    with DbSession(runtime.engine()) as db:
        users = db.scalars(select(User)).all()
    assert [u.email for u in users] == ["kt@trout.example"]

    # Another subject presenting that email cannot take the account over.
    monkeypatch.setattr(
        auth, "claims_from_callback", claims(sub="someone-else", email="kt@trout.example")
    )
    assert oidc.get("/auth/callback?code=x&state=y", follow_redirects=False).status_code == 409


def test_header_created_user_is_bound_on_first_login(oidc: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("IDENTITY_HEADER", "Tailscale-User-Login")
    oidc.get("/whoami", headers={"Tailscale-User-Login": "kilgore@kilgore.trout"})
    login(oidc)
    with DbSession(runtime.engine()) as db:
        users = db.scalars(select(User)).all()
    assert len(users) == 1 and users[0].subject == f"{ISSUER}|CgRtb2Nr"


def test_a_header_without_groups_never_wipes_memberships(oidc: TestClient, monkeypatch) -> None:
    login(oidc)  # groups: authors, lab
    monkeypatch.setenv("IDENTITY_HEADER", "Tailscale-User-Login")
    tailnet = TestClient(main.app)  # no session cookie; the header alone
    body = tailnet.get("/whoami", headers={"Tailscale-User-Login": "kilgore@kilgore.trout"}).json()
    assert body["source"] == "header" and body["groups"] == ["authors", "lab"]
    assert oidc.get("/whoami").json()["groups"] == ["authors", "lab"]


def test_session_cookie_is_secure_when_the_module_says_so(oidc: TestClient, monkeypatch) -> None:
    response = oidc.get("/auth/callback?code=x&state=y", follow_redirects=False)
    assert "secure" not in response.headers["set-cookie"].lower()
    monkeypatch.setenv("COOKIE_SECURE", "true")
    response = oidc.get("/auth/callback?code=x&state=y", follow_redirects=False)
    assert "secure" in response.headers["set-cookie"].lower()


def test_memberships_follow_the_idp_on_each_login(oidc: TestClient, monkeypatch) -> None:
    login(oidc)
    monkeypatch.setattr(auth, "claims_from_callback", claims(groups=["lab"]))
    login(oidc)
    assert oidc.get("/whoami").json()["groups"] == ["lab"]


def test_logout_revokes_the_session(oidc: TestClient) -> None:
    login(oidc)
    assert oidc.get("/whoami").json()["source"] == "session"

    assert oidc.post("/logout").json() == {"status": "logged out"}
    assert oidc.get("/whoami").json()["source"] is None
    with DbSession(runtime.engine()) as db:
        assert db.scalar(select(Session)) is None


# ------------------------------------------------------------------------------
# Behind oauth2-proxy (AUTH_PROXIED + IDENTITY_JWT_*): only a signed ID token
# ------------------------------------------------------------------------------

AUDIENCE = "oauth2-proxy"


@pytest.fixture
def proxied(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    key = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    public = KeySet([RSAKey.import_key(key.as_dict(private=False))])
    monkeypatch.setattr(auth, "_fetch_jwks", lambda issuer: public)
    monkeypatch.setattr(auth, "_jwks_cache", {})
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("AUTH_PROXIED", "1")
    monkeypatch.setenv("IDENTITY_HEADER", "X-Forwarded-Email")
    monkeypatch.setenv("IDENTITY_JWT_ISSUER", ISSUER)
    monkeypatch.setenv("IDENTITY_JWT_AUDIENCE", AUDIENCE)

    def token(**overrides) -> str:
        now = int(time.time())
        body = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "CgRhbm4",
            "email": "ann@lab.org",
            "email_verified": True,
            "groups": ["authors"],
            "iat": now,
            "exp": now + 600,
            **overrides,
        }
        return jwt.encode({"alg": "RS256", "kid": "test-key"}, body, key)

    return client, token


def test_proxied_webapp_trusts_a_verified_id_token(proxied) -> None:
    client, token = proxied
    body = client.get("/whoami", headers={"Authorization": f"Bearer {token()}"}).json()
    assert body["source"] == "token"
    assert body["login"] == "ann@lab.org" and body["groups"] == ["authors"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "someone-else"},
        {"iss": "https://evil.example"},
        {"exp": 1},
        {"email_verified": False},
        {"email_verified": None},
        {"sub": ""},
    ],
)
def test_proxied_webapp_rejects_bad_tokens(proxied, overrides) -> None:
    client, token = proxied
    headers = {"Authorization": f"Bearer {token(**overrides)}"}
    assert client.get("/whoami", headers=headers).json()["source"] is None


def test_proxied_webapp_ignores_bare_forwarded_headers(proxied) -> None:
    client, _ = proxied
    spoofed = {"X-Forwarded-Email": "admin@lab.org", "X-Forwarded-Groups": "platform"}
    assert client.get("/whoami", headers=spoofed).json()["source"] is None


def test_a_token_signed_by_another_key_is_refused(proxied) -> None:
    client, _ = proxied
    other = RSAKey.generate_key(2048, parameters={"kid": "test-key"})
    now = int(time.time())
    forged = jwt.encode(
        {"alg": "RS256", "kid": "test-key"},
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "email": "admin@lab.org", "exp": now + 600},
        other,
    )
    assert (
        client.get("/whoami", headers={"Authorization": f"Bearer {forged}"}).json()["source"]
        is None
    )


def test_token_users_are_keyed_by_subject(proxied) -> None:
    client, token = proxied

    def whoami(**overrides) -> dict:
        headers = {"Authorization": f"Bearer {token(**overrides)}"}
        return client.get("/whoami", headers=headers).json()

    assert whoami(sub="A")["login"] == "ann@lab.org"
    # Another account at the IdP asserting the same address does not become ann.
    assert whoami(sub="B")["source"] is None
    # ann's own account keeps her row when her address changes.
    assert whoami(sub="A", email="ann.smith@lab.org")["login"] == "ann.smith@lab.org"
    with DbSession(runtime.engine()) as db:
        user = db.scalar(select(User).where(User.subject == f"{ISSUER}|A"))
        assert user is not None and user.email == "ann.smith@lab.org"
        assert db.scalar(select(func.count()).select_from(User)) == 1


def test_only_rs256_tokens_are_accepted(proxied, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = proxied
    ec = ECKey.generate_key("P-256", parameters={"kid": "ec-key"})
    monkeypatch.setattr(
        auth, "_fetch_jwks", lambda issuer: KeySet([ECKey.import_key(ec.as_dict(private=False))])
    )
    monkeypatch.setattr(auth, "_jwks_cache", {})
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "x",
        "email": "ann@lab.org",
        "email_verified": True,
        "exp": now + 600,
    }
    # Signed by a key the issuer publishes, but not with the issuer's algorithm.
    es256 = jwt.encode({"alg": "ES256", "kid": "ec-key"}, claims, ec)
    headers = {"Authorization": f"Bearer {es256}"}
    assert client.get("/whoami", headers=headers).json()["source"] is None


# ------------------------------------------------------------------------------
# Different groups, different data
# ------------------------------------------------------------------------------


def test_group_greetings_are_visible_to_members_only(oidc: TestClient) -> None:
    anonymous = TestClient(main.app)
    # Public greeting: anyone.
    assert anonymous.post("/greetings", json={"name": "hello"}).status_code == 201
    # A group greeting needs a login ...
    assert (
        anonymous.post("/greetings", json={"name": "secret", "group": "authors"}).status_code == 401
    )

    login(oidc)  # groups: authors, lab
    # ... and membership.
    assert (
        oidc.post("/greetings", json={"name": "for authors", "group": "authors"}).status_code == 201
    )
    assert oidc.post("/greetings", json={"name": "nope", "group": "pipelines"}).status_code == 403

    # Members see public + their groups'; outsiders see public only.
    assert [g["name"] for g in oidc.get("/greetings").json()] == ["hello", "for authors"]
    assert [g["name"] for g in anonymous.get("/greetings").json()] == ["hello"]
    assert oidc.get("/").json()["greetings"] == 2
    assert anonymous.get("/").json()["greetings"] == 1

    # Ownership is recorded, and /greetings/mine wants a login.
    assert [g["name"] for g in oidc.get("/greetings/mine").json()] == ["for authors"]
    assert anonymous.get("/greetings/mine").status_code == 401


def test_require_group_dependency(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IDENTITY_HEADER", "Tailscale-User-Login")
    from typing import Annotated

    from fastapi import Depends, FastAPI

    gated = FastAPI()
    pipelines_only = auth.require_group("pipelines")

    @gated.get("/pipelines")
    def only_pipelines(identity: Annotated[auth.Identity, Depends(pipelines_only)]):
        return {"ok": identity.login}

    c = TestClient(gated)
    assert c.get("/pipelines").status_code == 401
    assert (
        c.get("/pipelines", headers={"Tailscale-User-Login": "a@b"}).status_code == 403
    )  # no groups in header mode without IDENTITY_GROUPS_HEADER


def test_identity_from_reads_lowercase_header_dicts(client: TestClient, monkeypatch) -> None:
    # The notebook sees its page request as a plain dict with lowercase header
    # names (mo.app_meta().request); identity_from must resolve it the same.
    monkeypatch.setenv("IDENTITY_HEADER", "Tailscale-User-Login")
    monkeypatch.setenv("IDENTITY_GROUPS_HEADER", "Tailscale-User-Groups")
    identity = auth.identity_from(
        {"tailscale-user-login": "ann@lab.org", "tailscale-user-groups": "authors"}, {}
    )
    assert identity is not None and identity.groups == ["authors"]
    assert auth.identity_from({}, {}) is None  # no cookie, no header: no DB touched


def test_notebook_hides_group_greetings_from_non_members(client: TestClient) -> None:
    # The notebook page reads through the same visibility rule as the API: run
    # as a script there is no page request, so the viewer is anonymous and
    # sees public greetings only.
    import importlib.util

    client.post("/greetings", json={"name": "hello"})
    with DbSession(runtime.engine()) as db:
        db.add(Greeting(name="for authors", group="authors"))
        db.commit()

    path = Path(main.__file__).parent / "notebooks" / "greetings.py"
    spec = importlib.util.spec_from_file_location("greetings_notebook", path)
    assert spec is not None and spec.loader is not None
    notebook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notebook)
    _, defs = notebook.app.run()

    assert defs["viewer_groups"] == []
    assert defs["names"] == ["hello"]


# ------------------------------------------------------------------------------
# App-managed groups (app:<name>): created and administered in the app, kept
# across the identity provider's syncs
# ------------------------------------------------------------------------------


@pytest.fixture
def tailnet(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Header mode: every request names its caller, and optionally their IdP groups."""
    monkeypatch.setenv("IDENTITY_HEADER", "X-Forwarded-Email")
    monkeypatch.setenv("IDENTITY_GROUPS_HEADER", "X-Forwarded-Groups")
    return client


def as_(email: str, *groups: str) -> dict[str, str]:
    headers = {"X-Forwarded-Email": email}
    if groups:
        headers["X-Forwarded-Groups"] = ",".join(groups)
    return headers


def groups_of(client: TestClient, email: str) -> list[str]:
    return client.get("/whoami", headers=as_(email)).json()["groups"]


def test_anyone_logged_in_creates_an_app_group_and_administers_it(tailnet: TestClient) -> None:
    assert tailnet.post("/groups", json={"name": "reviewers"}).status_code == 401

    created = tailnet.post("/groups", json={"name": "reviewers"}, headers=as_("ann@lab.org"))
    assert created.status_code == 201
    assert created.json() == {"name": "app:reviewers", "role": "admin"}
    assert groups_of(tailnet, "ann@lab.org") == ["app:reviewers"]
    assert tailnet.get("/groups", headers=as_("ann@lab.org")).json() == [
        {"name": "app:reviewers", "role": "admin"}
    ]
    assert tailnet.get("/groups", headers=as_("bob@lab.org")).json() == []

    again = tailnet.post("/groups", json={"name": "reviewers"}, headers=as_("bob@lab.org"))
    assert again.status_code == 409


@pytest.mark.parametrize("name", ["Reviewers", "app:reviewers", "lab/authors", "a,b", "", "9x"])
def test_app_group_names_are_bare_slugs(tailnet: TestClient, name: str) -> None:
    response = tailnet.post("/groups", json={"name": name}, headers=as_("ann@lab.org"))
    assert response.status_code == 422


def test_group_admins_manage_members(tailnet: TestClient) -> None:
    ann, bob, carl = as_("ann@lab.org"), as_("bob@lab.org"), as_("carl@lab.org")
    tailnet.post("/groups", json={"name": "reviewers"}, headers=ann)

    added = tailnet.post("/groups/reviewers/members", json={"email": "bob@lab.org"}, headers=ann)
    assert added.status_code == 201 and added.json()["role"] == "member"
    assert groups_of(tailnet, "bob@lab.org") == ["app:reviewers"]
    assert tailnet.get("/groups", headers=bob).json() == [
        {"name": "app:reviewers", "role": "member"}
    ]

    # A member is not an admin, and an outsider is neither.
    for caller in (bob, carl):
        assert tailnet.get("/groups/reviewers/members", headers=caller).status_code == 403
        assert (
            tailnet.post(
                "/groups/reviewers/members", json={"email": "carl@lab.org"}, headers=caller
            ).status_code
            == 403
        )
        assert (
            tailnet.delete("/groups/reviewers/members/ann@lab.org", headers=caller).status_code
            == 403
        )
    assert tailnet.get("/groups/nope/members", headers=ann).status_code == 404
    assert tailnet.get("/groups/NOPE/members", headers=ann).status_code == 404
    assert tailnet.get("/groups/reviewers/members").status_code == 401

    assert tailnet.get("/groups/reviewers/members", headers=ann).json() == [
        {"email": "ann@lab.org", "name": None, "role": "admin"},
        {"email": "bob@lab.org", "name": None, "role": "member"},
    ]

    # Promoting an existing member is an update (200), and makes them an admin.
    promoted = tailnet.post(
        "/groups/reviewers/members", json={"email": "bob@lab.org", "role": "admin"}, headers=ann
    )
    assert promoted.status_code == 200 and promoted.json()["role"] == "admin"
    assert (
        tailnet.post(
            "/groups/reviewers/members", json={"email": "carl@lab.org"}, headers=bob
        ).status_code
        == 201
    )

    assert tailnet.delete("/groups/reviewers/members/bob@lab.org", headers=ann).status_code == 204
    assert groups_of(tailnet, "bob@lab.org") == []
    assert tailnet.delete("/groups/reviewers/members/bob@lab.org", headers=ann).status_code == 404


def test_a_member_added_by_email_is_bound_on_first_login(oidc: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("IDENTITY_HEADER", "X-Forwarded-Email")
    admin = TestClient(main.app)
    admin.post("/groups", json={"name": "reviewers"}, headers=as_("ann@lab.org"))
    added = admin.post(
        "/groups/reviewers/members",
        json={"email": "kilgore@kilgore.trout"},
        headers=as_("ann@lab.org"),
    )
    assert added.status_code == 201
    with DbSession(runtime.engine()) as db:
        user = db.scalar(select(User).where(User.email == "kilgore@kilgore.trout"))
        assert user is not None and user.subject is None  # a bare row, never logged in

    login(oidc)  # groups: authors, lab
    with DbSession(runtime.engine()) as db:
        users = db.scalars(select(User).where(User.email == "kilgore@kilgore.trout")).all()
    assert len(users) == 1 and users[0].subject == f"{ISSUER}|CgRtb2Nr"
    assert oidc.get("/whoami").json()["groups"] == ["app:reviewers", "authors", "lab"]


def test_idp_syncs_keep_app_memberships(oidc: TestClient, monkeypatch) -> None:
    login(oidc)  # groups: authors, lab
    assert oidc.post("/groups", json={"name": "reviewers"}).status_code == 201

    # Each login replaces the IdP's memberships, and only those.
    monkeypatch.setattr(auth, "claims_from_callback", claims(groups=["lab"]))
    login(oidc)
    assert oidc.get("/whoami").json()["groups"] == ["app:reviewers", "lab"]
    monkeypatch.setattr(auth, "claims_from_callback", claims(groups=[]))
    login(oidc)
    assert oidc.get("/whoami").json()["groups"] == ["app:reviewers"]

    # So does a proxy's groups header (the same sync).
    monkeypatch.setenv("IDENTITY_HEADER", "X-Forwarded-Email")
    monkeypatch.setenv("IDENTITY_GROUPS_HEADER", "X-Forwarded-Groups")
    tailnet = TestClient(main.app)
    body = tailnet.get("/whoami", headers=as_("kilgore@kilgore.trout", "pipelines")).json()
    assert body["groups"] == ["app:reviewers", "pipelines"]
    with DbSession(runtime.engine()) as db:
        sources = sorted((m.group, m.source) for m in db.scalars(select(Membership)))
    assert sources == [("app:reviewers", "app"), ("pipelines", "idp")]


def test_the_idp_cannot_hand_out_app_groups(tailnet: TestClient) -> None:
    tailnet.post("/groups", json={"name": "reviewers"}, headers=as_("ann@lab.org"))
    spoofer = as_("mallory@lab.org", "app:reviewers", "/lab")
    assert tailnet.get("/whoami", headers=spoofer).json()["groups"] == ["/lab"]
    assert tailnet.get("/groups/reviewers/members", headers=spoofer).status_code == 403


def test_superadmins_administer_every_group(tailnet: TestClient, monkeypatch) -> None:
    tailnet.post("/groups", json={"name": "reviewers"}, headers=as_("ann@lab.org"))
    root = as_("root@lab.org", "/platform-admins")

    # Every group, whether or not they are in it.
    assert tailnet.get("/groups", headers=root).json() == [{"name": "app:reviewers", "role": None}]
    assert tailnet.get("/groups/reviewers/members", headers=root).status_code == 200
    added = tailnet.post("/groups/reviewers/members", json={"email": "bob@lab.org"}, headers=root)
    assert added.status_code == 201
    assert tailnet.delete("/groups/reviewers/members/bob@lab.org", headers=root).status_code == 204

    # APP_ADMIN_GROUP names another IdP group ...
    monkeypatch.setenv("APP_ADMIN_GROUP", "/lab/admins")
    assert tailnet.get("/groups/reviewers/members", headers=root).status_code == 403
    lab_admin = as_("lead@lab.org", "/lab/admins")
    assert tailnet.get("/groups/reviewers/members", headers=lab_admin).status_code == 200

    # ... but never an app group, which anyone could create and join.
    monkeypatch.setenv("APP_ADMIN_GROUP", "app:admins")
    mallory = as_("mallory@lab.org")
    tailnet.post("/groups", json={"name": "admins"}, headers=mallory)
    assert tailnet.get("/groups/reviewers/members", headers=mallory).status_code == 403


def test_superadmins_by_email_for_sources_without_groups(tailnet: TestClient, monkeypatch) -> None:
    tailnet.post("/groups", json={"name": "reviewers"}, headers=as_("ann@lab.org"))
    monkeypatch.setenv("APP_ADMIN_EMAILS", " Boss@Lab.org , other@lab.org")
    boss = as_("boss@lab.org")  # no groups header: groups are empty
    assert tailnet.get("/groups", headers=boss).json() == [{"name": "app:reviewers", "role": None}]
    assert tailnet.get("/groups/reviewers/members", headers=boss).status_code == 200
    assert tailnet.get("/groups/reviewers/members", headers=as_("bob@lab.org")).status_code == 403


def test_app_group_greetings_are_visible_to_members_only(tailnet: TestClient) -> None:
    ann, bob, carl = as_("ann@lab.org"), as_("bob@lab.org"), as_("carl@lab.org")
    tailnet.post("/groups", json={"name": "reviewers"}, headers=ann)
    tailnet.post("/groups/reviewers/members", json={"email": "bob@lab.org"}, headers=ann)
    tailnet.post("/greetings", json={"name": "hello"})

    for_reviewers = {"name": "for reviewers", "group": "app:reviewers"}
    assert tailnet.post("/greetings", json=for_reviewers, headers=ann).status_code == 201
    assert tailnet.post("/greetings", json=for_reviewers, headers=carl).status_code == 403

    def names(headers: dict[str, str] | None = None) -> list[str]:
        return [g["name"] for g in tailnet.get("/greetings", headers=headers).json()]

    assert names(bob) == ["hello", "for reviewers"]
    assert names(carl) == ["hello"]
    assert names() == ["hello"]
    assert tailnet.get("/", headers=bob).json()["greetings"] == 2

    tailnet.delete("/groups/reviewers/members/bob@lab.org", headers=ann)
    assert names(bob) == ["hello"]


# ------------------------------------------------------------------------------
# On PostgreSQL the database enforces the same rule (row-level security,
# migration 0004). Skipped unless TEST_POSTGRES_URL names a server whose user
# may create databases (CI's postgres service; see packages/db/tests/test_rls.py).
# ------------------------------------------------------------------------------


@pytest.fixture
def postgres(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    server = os.environ.get("TEST_POSTGRES_URL")
    if not server:
        pytest.skip("needs TEST_POSTGRES_URL (a PostgreSQL server)")
    from alembic import command
    from alembic.config import Config

    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    name = f"app_{secrets.token_hex(4)}"
    with admin.connect() as c:
        c.execute(text(f"CREATE DATABASE {name}"))
    try:
        _fresh_app(
            monkeypatch, make_url(server).set(database=name).render_as_string(hide_password=False)
        )
        command.upgrade(Config(str(ALEMBIC_INI)), "head")
        yield TestClient(main.app)
    finally:
        runtime.engine().dispose()
        runtime.reset_engine()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
        admin.dispose()


def test_on_postgres_the_api_reads_and_writes_under_row_security(
    postgres: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IDENTITY_HEADER", "X-Forwarded-Email")
    monkeypatch.setenv("IDENTITY_GROUPS_HEADER", "X-Forwarded-Groups")
    ann, bob = as_("ann@lab.org", "/lab/authors"), as_("bob@lab.org", "/acme/research")
    assert postgres.post("/greetings", json={"name": "hello"}).status_code == 201
    for_authors = {"name": "for authors", "group": "/lab/authors"}
    assert postgres.post("/greetings", json=for_authors, headers=ann).status_code == 201
    postgres.post("/groups", json={"name": "reviewers"}, headers=ann)
    for_reviewers = {"name": "for reviewers", "group": "app:reviewers"}
    assert postgres.post("/greetings", json=for_reviewers, headers=ann).status_code == 201

    def names(headers: dict[str, str] | None = None, path: str = "/greetings") -> list[str]:
        return [g["name"] for g in postgres.get(path, headers=headers).json()]

    everything = ["hello", "for authors", "for reviewers"]
    assert names(ann) == everything
    assert names(ann, "/greetings/mine") == ["for authors", "for reviewers"]
    assert postgres.get("/", headers=ann).json()["greetings"] == 3
    assert names(bob) == names() == ["hello"]

    # Without the code's filter, the database still holds the line.
    monkeypatch.setattr(main, "_visible_to", lambda identity: true())
    assert names(ann) == everything
    assert names(bob) == names() == ["hello"]
    assert postgres.get("/", headers=bob).json()["greetings"] == 1


def test_data_reports_the_refs_the_pod_received(monkeypatch: pytest.MonkeyPatch) -> None:
    # Needs no database and no store library: the contract is just parsed.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "DATA_REFS",
        '{"zarr/greetings": "s3://prod-data/tether/greetings.icechunk#tether.ws.6e117556.pr7",'
        ' "delta/greetings_log": "s3://prod-data/tether/greetings_log.delta@v12"}',
    )
    body = TestClient(main.app).get("/data").json()

    assert body["zarr/greetings"] == {
        "location": "s3://prod-data/tether/greetings.icechunk",
        "ref": "tether.ws.6e117556.pr7",
        "pinned": False,
    }
    assert body["delta/greetings_log"]["pinned"] is True
