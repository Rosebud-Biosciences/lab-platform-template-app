"""Who is calling, and what they may see.

The workloads module tells the app how identity reaches it (AUTH_MODE), and
the app accepts exactly that source -- nothing else:

- ``oidc``: the app is its own relying party against the platform's issuer
  (Dex, or any other), using the OIDC_* env the module hands it: ``/login`` ->
  the issuer -> ``/auth/callback``. The user (keyed by issuer and subject),
  their group memberships (from the token's groups claim) and a server-side
  session are written to the app's database. When the webapp itself sits
  behind the platform's oauth2-proxy (AUTH_PROXIED=1), the proxy's ID token
  (``Authorization: Bearer``) is verified against the issuer's keys instead,
  so identity never rests on the network path; its user is keyed the same
  way.
- ``headers``: a proxy that already authenticated the caller (the tailnet's
  Ingress) sets the header IDENTITY_HEADER names, and optionally
  IDENTITY_GROUPS_HEADER. Trust it only where that proxy is the sole route to
  the pod (the module's NetworkPolicy enforces that where the CNI does).
- ``none``: nobody is identified.

With AUTH_MODE unset (a laptop, the tests) the app accepts a login when OIDC_*
is set and a header when IDENTITY_HEADER is set -- and no header otherwise.

Sessions: the cookie carries a random id; the database stores only
HMAC(SESSION_SECRET, id). Preview databases are branches of prod's, so prod's
session rows reach every preview -- hashed with prod's secret, which previews
do not have, they are useless there (and preview-up purges them anyway).

Authorization is by group: ``require_group("pipelines")`` as a dependency, and
queries that filter on the caller's groups (see main.py's greetings).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx2
from authlib.integrations.starlette_client import OAuth
from db.models import Membership, Session, User
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload

from app.runtime import engine

SESSION_COOKIE = "app_session"
SESSION_TTL = timedelta(hours=24)
# How stale last_login_at may get before a header/token request refreshes it:
# identity arrives on every request in those modes, and a write per request
# is not worth an accurate timestamp.
LAST_SEEN_RESOLUTION = timedelta(hours=1)

# Without SESSION_SECRET (a laptop), sessions are keyed by a per-process key
# and simply do not survive a restart. The module always sets it.
_EPHEMERAL_KEY = secrets.token_bytes(32)


def _flag(name: str) -> bool | None:
    value = os.environ.get(name, "").strip().lower()
    if value in ("1", "true", "yes"):
        return True
    if value in ("0", "false", "no"):
        return False
    return None


@dataclass(frozen=True)
class Settings:
    auth_mode: str  # "oidc" | "headers" | "none" | "" (unset)
    proxied: bool
    issuer_url: str
    client_id: str
    client_secret: str
    redirect_url: str
    scopes: str
    groups_claim: str
    identity_header: str
    identity_groups_header: str
    identity_name_header: str
    jwt_issuer: str
    jwt_audience: str
    cookie_secure: bool | None

    @classmethod
    def from_env(cls) -> Settings:
        identity_header = os.environ.get("IDENTITY_HEADER", "")
        return cls(
            auth_mode=os.environ.get("AUTH_MODE", "").strip().lower(),
            proxied=_flag("AUTH_PROXIED") is True,
            issuer_url=os.environ.get("OIDC_ISSUER_URL", "").rstrip("/"),
            client_id=os.environ.get("OIDC_CLIENT_ID", ""),
            client_secret=os.environ.get("OIDC_CLIENT_SECRET", ""),
            redirect_url=os.environ.get("OIDC_REDIRECT_URL", ""),
            scopes=os.environ.get("OIDC_SCOPES", "openid email profile groups"),
            groups_claim=os.environ.get("OIDC_GROUPS_CLAIM", "groups"),
            identity_header=identity_header,
            identity_groups_header=os.environ.get("IDENTITY_GROUPS_HEADER", ""),
            # Tailscale sends the display name alongside the login; other
            # proxies name their own header or send none.
            identity_name_header=os.environ.get(
                "IDENTITY_NAME_HEADER",
                "Tailscale-User-Name" if identity_header.lower() == "tailscale-user-login" else "",
            ),
            jwt_issuer=os.environ.get("IDENTITY_JWT_ISSUER", "").rstrip("/"),
            jwt_audience=os.environ.get("IDENTITY_JWT_AUDIENCE", ""),
            cookie_secure=_flag("COOKIE_SECURE"),
        )

    @property
    def oidc_configured(self) -> bool:
        return bool(self.issuer_url and self.client_id)

    @property
    def login_enabled(self) -> bool:
        """The app runs its own login (and honours its session cookie)."""
        return self.auth_mode in ("oidc", "") and self.oidc_configured

    @property
    def sessions_enabled(self) -> bool:
        return self.auth_mode in ("oidc", "")

    @property
    def verify_tokens(self) -> bool:
        """Behind oauth2-proxy with the issuer named: trust only a signed ID token."""
        return self.proxied and bool(self.jwt_issuer and self.jwt_audience)

    @property
    def trust_headers(self) -> bool:
        if not self.identity_header or self.verify_tokens:
            return False
        return self.auth_mode in ("headers", "") or self.proxied


def settings() -> Settings:
    return Settings.from_env()


@dataclass(frozen=True)
class Identity:
    """The resolved caller: a user row plus how it was established."""

    user: User
    source: str  # "session" | "token" | "header"
    groups: list[str] = field(default_factory=list)

    @property
    def login(self) -> str:
        return self.user.email


class AccountConflict(Exception):
    """An email already belongs to a user of another identity-provider subject."""


# ------------------------------------------------------------------------------
# Users (keyed by issuer|sub; email is an attribute)
# ------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _load(db: DbSession, *where: Any) -> User | None:
    return db.scalar(select(User).options(selectinload(User.memberships)).where(*where))


def _sync_memberships(user: User, groups: list[str]) -> bool:
    """Make the user's memberships exactly `groups`; True if anything changed."""
    wanted = set(groups)
    changed = False
    for m in list(user.memberships):
        if m.group not in wanted:
            user.memberships.remove(m)
            changed = True
    have = {m.group for m in user.memberships}
    for g in sorted(wanted - have):
        user.memberships.append(Membership(group=g))
        changed = True
    return changed


def _create_by_email(email: str) -> None:
    """Insert a bare user row in its own transaction, tolerating a racing insert.

    Two first requests for the same person can arrive together; the unique
    email lets exactly one insert win, and the loser simply re-reads.
    """
    with DbSession(engine()) as other:
        other.add(User(email=email))
        try:
            other.commit()
        except IntegrityError:
            other.rollback()


def _user_for_subject(db: DbSession, *, subject: str, email: str) -> tuple[User, bool]:
    """The user of an identity-provider subject -- found, bound or new -- and whether it changed.

    A row found only by email is bound to this subject if it has none yet (it
    was created from an identity header, or added by email before the person
    ever logged in). A row whose email matches but whose subject differs is a
    different account at the identity provider: refuse rather than take it over.
    """
    user = _load(db, User.subject == subject)
    if user is None:
        user = _load(db, User.email == email)
        if user is not None and user.subject not in (None, subject):
            raise AccountConflict(email)
        if user is None:
            user = User(email=email)
            db.add(user)
        user.subject = subject
        return user, True
    if user.email != email:
        if _load(db, User.email == email) is not None:
            raise AccountConflict(email)
        user.email = email
        return user, True
    return user, False


def record_oidc_login(
    db: DbSession, *, subject: str, email: str, name: str | None, groups: list[str]
) -> User:
    """A verified login: find the user by subject, bind or create, sync groups."""
    user, _ = _user_for_subject(db, subject=subject, email=email)
    if name:
        user.name = name
    user.last_login_at = _now()
    _sync_memberships(user, groups)
    db.flush()
    return user


def record_token_identity(
    db: DbSession, *, subject: str, email: str, name: str | None, groups: list[str]
) -> User:
    """A verified ID token on this request (oauth2-proxy's): keyed by subject, as a login is.

    Writes only when something changed, as record_asserted_identity does.
    """
    try:
        return _write_token_identity(db, subject=subject, email=email, name=name, groups=groups)
    except IntegrityError:
        # A racing first request for the same person inserted the row first.
        db.rollback()
        return _write_token_identity(db, subject=subject, email=email, name=name, groups=groups)


def _write_token_identity(
    db: DbSession, *, subject: str, email: str, name: str | None, groups: list[str]
) -> User:
    user, changed = _user_for_subject(db, subject=subject, email=email)
    if name and user.name != name:
        user.name = name
        changed = True
    if _sync_memberships(user, groups):
        changed = True
    if user.last_login_at is None or _now() - user.last_login_at > LAST_SEEN_RESOLUTION:
        user.last_login_at = _now()
        changed = True
    if changed:
        db.commit()
        db.refresh(user)
    return user


def record_asserted_identity(
    db: DbSession, *, email: str, name: str | None, groups: list[str] | None
) -> User:
    """An identity a proxy asserted on this request in a header.

    Writes only when something changed: a new user, a new display name, a
    membership change, or a last_login_at older than LAST_SEEN_RESOLUTION.
    `groups=None` means the source carries no groups: memberships are left
    alone rather than wiped (a user's groups from an earlier OIDC login, or
    from the app itself, survive a request that arrives through the tailnet).
    """
    user = _load(db, User.email == email)
    if user is None:
        _create_by_email(email)
        db.expire_all()
        user = _load(db, User.email == email)
        assert user is not None
    changed = False
    if name and user.name != name:
        user.name = name
        changed = True
    if groups is not None and _sync_memberships(user, groups):
        changed = True
    if user.last_login_at is None or _now() - user.last_login_at > LAST_SEEN_RESOLUTION:
        user.last_login_at = _now()
        changed = True
    if changed:
        db.commit()
        db.refresh(user)
    return user


# ------------------------------------------------------------------------------
# Sessions: the cookie holds a random id, the row holds HMAC(SESSION_SECRET, id)
# ------------------------------------------------------------------------------


def _session_key() -> bytes:
    secret = os.environ.get("SESSION_SECRET", "")
    return secret.encode() if secret else _EPHEMERAL_KEY


def session_row_id(sid: str) -> str:
    return hmac.new(_session_key(), sid.encode(), hashlib.sha256).hexdigest()


def create_session(db: DbSession, user: User) -> str:
    sid = secrets.token_urlsafe(32)
    db.add(Session(id=session_row_id(sid), user_id=user.id, expires_at=_now() + SESSION_TTL))
    db.flush()
    return sid


def user_for_session(db: DbSession, sid: str) -> User | None:
    row = db.scalar(
        select(Session)
        .options(selectinload(Session.user).selectinload(User.memberships))
        .where(Session.id == session_row_id(sid), Session.expires_at > _now())
    )
    return row.user if row else None


# ------------------------------------------------------------------------------
# ID tokens forwarded by oauth2-proxy (AUTH_PROXIED=1 + IDENTITY_JWT_*)
# ------------------------------------------------------------------------------

_JWKS_TTL_SECONDS = 300
_jwks_cache: dict[str, tuple[float, KeySet]] = {}


def _fetch_jwks(issuer: str) -> KeySet:
    meta = httpx2.get(f"{issuer}/.well-known/openid-configuration", timeout=5).json()
    return KeySet.import_key_set(httpx2.get(meta["jwks_uri"], timeout=5).json())


def _jwks(issuer: str, *, refresh: bool = False) -> KeySet:
    cached = _jwks_cache.get(issuer)
    if refresh or cached is None or time.monotonic() - cached[0] > _JWKS_TTL_SECONDS:
        _jwks_cache[issuer] = (time.monotonic(), _fetch_jwks(issuer))
    return _jwks_cache[issuer][1]


def verify_id_token(token: str, cfg: Settings) -> dict[str, Any] | None:
    """The token's claims if the issuer signed it for this audience; else None."""
    registry = jwt.JWTClaimsRegistry(
        iss={"essential": True, "value": cfg.jwt_issuer},
        aud={"essential": True, "value": cfg.jwt_audience},
        exp={"essential": True},
        email={"essential": True},
    )
    for refresh in (False, True):  # one refetch covers a key rotation
        try:
            # Dex signs RS256; nothing else is accepted, whatever the header says.
            decoded = jwt.decode(
                token, _jwks(cfg.jwt_issuer, refresh=refresh), algorithms=["RS256"]
            )
            registry.validate(decoded.claims)
            return dict(decoded.claims)
        except JoseError:
            continue
        except httpx2.HTTPError, KeyError, ValueError:
            return None
    return None


# ------------------------------------------------------------------------------
# The caller (FastAPI dependencies)
# ------------------------------------------------------------------------------


def _groups_from_claims(claims: Mapping[str, Any], claim: str) -> list[str]:
    groups = claims.get(claim) or []
    if isinstance(groups, str):
        groups = [groups]
    return [str(g) for g in groups]


def identity_from(headers: Mapping[str, str], cookies: Mapping[str, str]) -> Identity | None:
    """Resolve the caller from a request's headers and cookies.

    Shared by the API (`current_identity`) and the notebook pages, which see
    the page request through `mo.app_meta().request` -- a plain dict whose
    header names are lowercase, hence the case-insensitive lookup.
    """
    cfg = settings()
    if cfg.auth_mode == "none":
        return None
    lowered = {k.lower(): v for k, v in headers.items()}

    def header(name: str) -> str | None:
        return lowered.get(name.lower()) if name else None

    sid = cookies.get(SESSION_COOKIE) if cfg.sessions_enabled else None
    bearer = header("authorization") or ""
    token = bearer[7:].strip() if cfg.verify_tokens and bearer[:7].lower() == "bearer " else ""
    login = header(cfg.identity_header) if cfg.trust_headers else None
    if not (sid or token or login):
        return None

    with DbSession(engine()) as db:
        if sid:
            user = user_for_session(db, sid)
            if user is not None:
                return Identity(user=user, source="session", groups=user.groups)

        if token:
            claims = verify_id_token(token, cfg)
            # The same bar as a login: a verified email and a subject to key on.
            if claims is None or claims.get("email_verified") is not True or not claims.get("sub"):
                return None
            try:
                user = record_token_identity(
                    db,
                    subject=f"{cfg.jwt_issuer}|{claims['sub']}",
                    email=str(claims["email"]),
                    name=claims.get("name"),
                    groups=_groups_from_claims(claims, cfg.groups_claim),
                )
            except AccountConflict:
                return None
            return Identity(user=user, source="token", groups=user.groups)

        if login:
            groups_header = header(cfg.identity_groups_header)
            groups = (
                [g.strip() for g in groups_header.split(",") if g.strip()]
                if cfg.identity_groups_header and groups_header is not None
                else None
            )
            user = record_asserted_identity(
                db, email=login, name=header(cfg.identity_name_header), groups=groups
            )
            return Identity(user=user, source="header", groups=user.groups)
    return None


def current_identity(request: Request) -> Identity | None:
    return identity_from(request.headers, request.cookies)


CurrentIdentity = Annotated[Identity | None, Depends(current_identity)]


def require_user(identity: CurrentIdentity) -> Identity:
    if identity is None:
        raise HTTPException(status_code=401, detail="login required")
    return identity


def require_group(group: str):
    """Dependency factory: the caller must be a member of `group`."""

    def check(identity: Annotated[Identity, Depends(require_user)]) -> Identity:
        if group not in identity.groups:
            raise HTTPException(status_code=403, detail=f"requires group {group!r}")
        return identity

    return check


# ------------------------------------------------------------------------------
# OIDC login (Authlib's Starlette client; needs SessionMiddleware for the
# short-lived OAuth state, see main.py)
# ------------------------------------------------------------------------------

router = APIRouter()
_oauth: OAuth | None = None


def _client() -> Any:
    """The registered OIDC client, built on first use from the env."""
    global _oauth
    cfg = settings()
    if not cfg.login_enabled:
        raise HTTPException(
            status_code=501,
            detail="OIDC login is not enabled (AUTH_MODE, OIDC_ISSUER_URL, OIDC_CLIENT_ID)",
        )
    if _oauth is None:
        _oauth = OAuth()
        _oauth.register(
            "idp",
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            server_metadata_url=f"{cfg.issuer_url}/.well-known/openid-configuration",
            # PKCE: the code is useless to anyone who intercepts the redirect.
            client_kwargs={"scope": cfg.scopes, "code_challenge_method": "S256"},
        )
    return _oauth.idp


async def claims_from_callback(request: Request) -> dict[str, Any]:
    """Finish the authorization-code flow and return the ID token's claims.

    Split out so tests can stand in for the issuer without a network.
    """
    client = _client()
    token = await client.authorize_access_token(request)
    claims = token.get("userinfo")
    if not claims:
        claims = await client.userinfo(token=token)
    return dict(claims)


def safe_next(target: str) -> str:
    """Only same-origin paths: `//host` and `/\\host` are other origins to a browser."""
    target = target or "/"
    parts = urlsplit(target)
    if (
        not target.startswith("/")
        or target.startswith(("//", "/\\"))
        or parts.scheme
        or parts.netloc
    ):
        return "/"
    return target


def cookie_secure(request: Request) -> bool:
    cfg = settings()
    return cfg.cookie_secure if cfg.cookie_secure is not None else request.url.scheme == "https"


@router.get("/login")
async def login(request: Request, next: str = "/"):
    cfg = settings()
    client = _client()
    request.session["next"] = safe_next(next)
    return await client.authorize_redirect(request, cfg.redirect_url or None)


@router.get("/auth/callback")
async def callback(request: Request):
    cfg = settings()
    claims = await claims_from_callback(request)
    email = claims.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="the identity provider sent no email claim")
    if claims.get("email_verified") is not True:
        raise HTTPException(
            status_code=403, detail="the identity provider has not verified this email"
        )
    subject = claims.get("sub")
    if not subject:
        raise HTTPException(status_code=400, detail="the identity provider sent no subject")

    with DbSession(engine()) as db:
        try:
            user = record_oidc_login(
                db,
                subject=f"{cfg.issuer_url}|{subject}",
                email=str(email),
                name=claims.get("name"),
                groups=_groups_from_claims(claims, cfg.groups_claim),
            )
        except AccountConflict:
            raise HTTPException(
                status_code=409,
                detail="this email belongs to another account at the identity provider",
            ) from None
        sid = create_session(db, user)
        db.commit()

    response = RedirectResponse(safe_next(request.session.pop("next", "/")), status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request),
        max_age=int(SESSION_TTL.total_seconds()),
    )
    return response


@router.post("/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    """End the app's session.

    No CSRF token: the session cookie is SameSite=Lax, so browsers do not send
    it on a cross-site POST. This logs out of the app only; the identity
    provider's session (and Dex's, once a Dex release ships sessions) stays.
    """
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        with DbSession(engine()) as db:
            db.execute(delete(Session).where(Session.id == session_row_id(sid)))
            db.commit()
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "logged out"}
