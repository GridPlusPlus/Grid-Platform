"""Link platform users to an immutable Gitea identity.

Revision ID: 20261009_0005
Revises: 20260817_0004
Create Date: 2026-10-09
"""

from alembic import op
import sqlalchemy as sa


revision = "20261009_0005"
down_revision = "20260817_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("gitea_user_id", sa.Text(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_users_gitea_user_id", ["gitea_user_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("uq_users_gitea_user_id", type_="unique")
        batch_op.drop_column("gitea_user_id")
