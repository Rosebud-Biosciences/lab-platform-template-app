"""users, sessions, memberships; greeting ownership and group

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-18

The app's own auth state (who logged in, their sessions, their groups) lives
in this database on purpose: it forks with a preview along with the data it
gates, and the identity provider stays the source of truth for identity.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("subject", name="uq_users_subject"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("group_name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), server_default="member", nullable=False),
        sa.UniqueConstraint("user_id", "group_name", name="uq_memberships_user_group"),
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_index("ix_memberships_group_name", "memberships", ["group_name"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    with op.batch_alter_table("greetings") as batch:
        batch.add_column(sa.Column("owner_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("group_name", sa.String(), nullable=True))
        batch.create_foreign_key(
            "fk_greetings_owner_id_users", "users", ["owner_id"], ["id"], ondelete="SET NULL"
        )
    op.create_index("ix_greetings_group_name", "greetings", ["group_name"])


def downgrade() -> None:
    op.drop_index("ix_greetings_group_name", table_name="greetings")
    with op.batch_alter_table("greetings") as batch:
        batch.drop_constraint("fk_greetings_owner_id_users", type_="foreignkey")
        batch.drop_column("group_name")
        batch.drop_column("owner_id")

    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_memberships_group_name", table_name="memberships")
    op.drop_index("ix_memberships_user_id", table_name="memberships")
    op.drop_table("memberships")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
