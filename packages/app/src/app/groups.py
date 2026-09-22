"""App-managed groups: the app's own, next to the identity provider's.

The identity provider decides who is in ITS groups (``/lab/authors``, synced
on every login); these are groups the app's users make for themselves -- a
review panel, a reading club -- stored and shown as ``app:<name>``. Anyone
logged in may create one and becomes its admin; its admins and the
platform's superadmins (``auth.Identity.is_superadmin``) manage its members.
An app group works wherever a group name does: a greeting's group,
``auth.require_group("app:reviewers")``. Like every other auth row they live
in the app's database, so a preview's groups are the preview's.

Paths take the bare name (``/groups/reviewers/members``); bodies of
``POST /groups`` too; responses carry the full ``app:reviewers``.
"""

import re
from typing import Annotated, Literal

from db.models import APP_GROUP_PREFIX, Group, Membership, User
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app import auth
from app.auth import Identity, require_user
from app.runtime import engine

# No ":" (the prefix is the app's to add), no "/" (IdP groups are paths), no
# "," (group lists travel comma-joined).
NAME_PATTERN = r"^[a-z][a-z0-9_-]{0,40}$"

router = APIRouter(prefix="/groups", tags=["groups"])

Caller = Annotated[Identity, Depends(require_user)]


class GroupIn(BaseModel):
    name: Annotated[str, Field(pattern=NAME_PATTERN)]


class MemberIn(BaseModel):
    email: Annotated[str, Field(pattern=r"^[^@\s]+@[^@\s]+$")]
    role: Literal["member", "admin"] = "member"


def _group(db: DbSession, name: str) -> Group:
    """The group at /groups/{name}; 404 if there is none."""
    group = (
        db.scalar(select(Group).where(Group.name == APP_GROUP_PREFIX + name))
        if re.fullmatch(NAME_PATTERN, name)
        else None
    )
    if group is None:
        raise HTTPException(status_code=404, detail=f"no group {name!r}")
    return group


def _membership(db: DbSession, group: Group, *where) -> Membership | None:
    return db.scalar(
        select(Membership)
        .join(Membership.user)
        .where(Membership.group_id == group.id, Membership.source == "app", *where)
    )


def _administered(db: DbSession, name: str, identity: Identity) -> Group:
    """The group, if the caller may manage it (its admin, or a superadmin); else 403."""
    group = _group(db, name)
    if identity.is_superadmin:
        return group
    mine = _membership(db, group, Membership.user_id == identity.user.id)
    if mine is None or mine.role != "admin":
        raise HTTPException(status_code=403, detail=f"requires admin of {group.name!r}")
    return group


@router.post("", status_code=201)
def create_group(body: GroupIn, identity: Caller) -> dict:
    name = APP_GROUP_PREFIX + body.name
    with DbSession(engine()) as db:
        group = Group(name=name, created_by=identity.user.id)
        group.memberships.append(
            Membership(user_id=identity.user.id, group=name, source="app", role="admin")
        )
        db.add(group)
        try:
            db.commit()
        except IntegrityError:
            raise HTTPException(status_code=409, detail=f"{name!r} already exists") from None
    return {"name": name, "role": "admin"}


@router.get("")
def list_groups(identity: Caller) -> list[dict]:
    """The app groups the caller belongs to, with their role in each.

    A superadmin gets every group (role null where they are not a member).
    The IdP's groups are not listed here: /whoami has all of the caller's.
    """
    with DbSession(engine()) as db:
        mine = (
            select(Membership.group_id, Membership.role)
            .where(Membership.user_id == identity.user.id, Membership.source == "app")
            .subquery()
        )
        query = select(Group.name, mine.c.role).order_by(Group.name)
        if identity.is_superadmin:
            query = query.outerjoin(mine, mine.c.group_id == Group.id)
        else:
            query = query.join(mine, mine.c.group_id == Group.id)
        return [{"name": name, "role": role} for name, role in db.execute(query)]


@router.get("/{name}/members")
def list_members(name: str, identity: Caller) -> list[dict]:
    with DbSession(engine()) as db:
        group = _administered(db, name, identity)
        rows = db.execute(
            select(User.email, User.name, Membership.role)
            .join(Membership.user)
            .where(Membership.group_id == group.id, Membership.source == "app")
            .order_by(User.email)
        )
        return [{"email": email, "name": n, "role": role} for email, n, role in rows]


@router.post("/{name}/members", status_code=201)
def add_member(name: str, body: MemberIn, identity: Caller, response: Response) -> dict:
    """Add someone by email, or change their role (200 when they were a member already).

    Someone who never logged in gets a bare user row, bound to their
    identity-provider subject on their first OIDC login.
    """
    with DbSession(engine()) as db:
        group = _administered(db, name, identity)
        user = auth.user_by_email(db, body.email)
        membership = _membership(db, group, Membership.user_id == user.id)
        if membership is None:
            membership = Membership(
                user_id=user.id, group=group.name, group_id=group.id, source="app"
            )
            db.add(membership)
        else:
            response.status_code = 200
        membership.role = body.role
        out = {"email": user.email, "name": user.name, "role": membership.role}
        db.commit()
    return out


@router.delete("/{name}/members/{email}", status_code=204)
def remove_member(name: str, email: str, identity: Caller) -> None:
    with DbSession(engine()) as db:
        group = _administered(db, name, identity)
        membership = _membership(db, group, User.email == email)
        if membership is None:
            raise HTTPException(
                status_code=404, detail=f"{email!r} is not a member of {group.name!r}"
            )
        db.delete(membership)
        db.commit()
