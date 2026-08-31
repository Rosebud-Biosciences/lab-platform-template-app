"""create greetings table

Revision ID: 0001
Revises:
Create Date: 2026-08-31

"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "greetings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_greetings_name", "greetings", ["name"])


def downgrade() -> None:
    op.drop_index("ix_greetings_name", table_name="greetings")
    op.drop_table("greetings")
