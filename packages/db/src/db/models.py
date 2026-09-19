from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import ColumnElement, ForeignKey, UniqueConstraint, func, or_
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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
        return sorted(m.group for m in self.memberships)


class Membership(Base):
    """A user's group, as the identity provider last reported it.

    Synced from the token's groups claim (or the proxy's groups header) on
    every login, so the IdP stays the source of truth for WHO is in a group
    while the app's tables decide WHAT a group may see -- and both the
    decision and the data it gates branch with the environment.
    """

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "group_name", name="uq_memberships_user_group"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # "group" is reserved in SQL; the attribute keeps the natural name.
    group: Mapped[str] = mapped_column("group_name", index=True)
    role: Mapped[str] = mapped_column(default="member", server_default="member")

    user: Mapped[User] = relationship(back_populates="memberships")


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
