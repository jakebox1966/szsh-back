"""detail view daily limit: 카운터(int) → 슬러그 집합(text[])

같은 회사는 하루 1번만 차감(distinct). detail_views_today 제거하고 detail_view_slugs 추가.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column(
            "detail_view_slugs",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    op.drop_column("users", "detail_views_today")


def downgrade():
    op.add_column(
        "users",
        sa.Column("detail_views_today", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_column("users", "detail_view_slugs")
