from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import CheckConstraint, ColumnElement, ForeignKey, UniqueConstraint, func, or_
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# App-managed group names carry this prefix everywhere -- stored, compared,
# shown -- so an app group can never pass for an identity-provider group
# (those are paths: "/lab/authors"), nor an IdP group for an app one.
APP_GROUP_PREFIX = "app:"


class Base(DeclarativeBase):
    pass


class User(Base):
    """Someone the app has seen log in.

    Identity is the identity provider's (Dex, Google, the tailnet): the app
    only records who showed up and which groups they were in at the time.
    Because these rows live in the app's own database, a preview's users are
    the preview's -- they branch with it and never touch prod.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    # "<issuer>|<sub>" for an OIDC login; None when the identity came from a
    # proxy header (tailnet mode), where the email is all there is.
    subject: Mapped[str | None] = mapped_column(unique=True)
    name: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_login_at: Mapped[datetime | None]

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def groups(self) -> list[str]:
        """The identity provider's groups plus the app's own (`app:` names)."""
        return sorted({m.group for m in self.memberships})


class Group(Base):
    """A group the app manages itself, next to the identity provider's.

    Anyone logged in may create one and becomes its admin; its admins (and
    the platform's superadmins) add and remove members. `name` is stored
    with APP_GROUP_PREFIX ("app:reviewers"), so it works wherever a group name
    does -- a greeting's group, `require_group` -- without ever colliding
    with a group the IdP vouches for.
    """

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="app_group", cascade="all, delete-orphan", passive_deletes=True
    )


class Membership(Base):
    """A user's group: as the identity provider last reported it, or as the app granted it.

    `source` "idp" rows are synced from the token's groups claim (or the
    proxy's groups header) on every login, so the IdP stays the source of
    truth for WHO is in its groups while the app's tables decide WHAT a group
    may see -- and both the decision and the data it gates branch with the
    environment. `source` "app" rows are memberships of an app-managed
    `Group` (`group_id`); the sync never touches them.
    """

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "group_name", "source", name="uq_memberships_user_group_source"
        ),
        CheckConstraint("source IN ('idp', 'app')", name="ck_memberships_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # "group" is reserved in SQL; the attribute keeps the natural name.
    group: Mapped[str] = mapped_column("group_name", index=True)
    role: Mapped[str] = mapped_column(default="member", server_default="member")
    source: Mapped[str] = mapped_column(default="idp", server_default="idp")
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), index=True
    )

    user: Mapped[User] = relationship(back_populates="memberships")
    app_group: Mapped[Group | None] = relationship(back_populates="memberships")


class Session(Base):
    """A login session: an opaque cookie value that maps to a user.

    Server-side (a row, not a signed token) so it can be revoked, listed, and
    -- like everything else here -- forked with a preview.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]

    user: Mapped[User] = relationship()


class Greeting(Base):
    """The entire hello-world domain model: one row per greeting.

    `owner_id` says who wrote it; `group` (nullable) says which group it is
    for -- a greeting with no group is public, one with a group is visible
    only to that group's members. The smallest possible "different groups see
    different data".
    """

    __tablename__ = "greetings"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    group: Mapped[str | None] = mapped_column("group_name", index=True)

    @classmethod
    def visible_to(cls, groups: Iterable[str]) -> ColumnElement[bool]:
        """The one visibility rule: public greetings plus those of `groups`.

        Every reader applies it -- the API and the notebook pages alike -- so
        no page shows a group's rows to a non-member.
        """
        groups = list(groups)
        if groups:
            return or_(cls.group.is_(None), cls.group.in_(groups))
        return cls.group.is_(None)
