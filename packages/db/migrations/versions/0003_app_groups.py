"""app-managed groups; memberships say where they came from

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-22

Groups the app itself manages (`app:<name>`, administered in the app) live
next to the identity provider's, in the same memberships table: `source` tells
an IdP-synced row ("idp", replaced on every login) from an app-granted one
("app", never touched by the sync), and `group_id` ties the latter to its
group so deleting a group deletes its memberships.
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_groups_created_by_users"),
            nullable=True,
        ),
        sa.UniqueConstraint("name", name="uq_groups_name"),
    )

    with op.batch_alter_table("memberships") as batch:
        batch.add_column(sa.Column("source", sa.String(), server_default="idp", nullable=False))
        batch.add_column(sa.Column("group_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_memberships_group_id_groups", "groups", ["group_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_check_constraint("ck_memberships_source", "source IN ('idp', 'app')")
        batch.drop_constraint("uq_memberships_user_group", type_="unique")
        batch.create_unique_constraint(
            "uq_memberships_user_group_source", ["user_id", "group_name", "source"]
        )
    op.create_index("ix_memberships_group_id", "memberships", ["group_id"])


def downgrade() -> None:
    # 0002 has no notion of app groups: its sync would treat these rows as the
    # IdP's until the next login, so they go with the groups table.
    op.execute("DELETE FROM memberships WHERE source = 'app'")
    op.drop_index("ix_memberships_group_id", table_name="memberships")
    with op.batch_alter_table("memberships") as batch:
        batch.drop_constraint("uq_memberships_user_group_source", type_="unique")
        batch.create_unique_constraint("uq_memberships_user_group", ["user_id", "group_name"])
        batch.drop_constraint("ck_memberships_source", type_="check")
        batch.drop_constraint("fk_memberships_group_id_groups", type_="foreignkey")
        batch.drop_column("group_id")
        batch.drop_column("source")
    op.drop_table("groups")
