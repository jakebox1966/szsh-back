"""add updated_at, deleted_at to company_comments

Revision ID: a3b1c2d4e5f6
Revises: 9c9ef722eed4
Create Date: 2026-05-11

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a3b1c2d4e5f6"
down_revision: Union[str, None] = "9c9ef722eed4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "company_comments",
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "company_comments",
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_company_comments_deleted_at",
        "company_comments",
        ["deleted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_company_comments_deleted_at", table_name="company_comments")
    op.drop_column("company_comments", "deleted_at")
    op.drop_column("company_comments", "updated_at")
