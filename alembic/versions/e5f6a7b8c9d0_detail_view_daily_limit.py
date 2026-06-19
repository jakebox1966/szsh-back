"""detail view daily limit columns (users.detail_views_today / reset_at)

상세페이지 일일 조회 한도(회원) 카운터. 검색이 아니라 회사 상세 진입 시점에 차감.

Revision ID: e5f6a7b8c9d0
Revises: d3e4f5a6b7c8
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "e5f6a7b8c9d0"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade():
    # server_default="0" — 기존 row 도 NOT NULL 만족하도록.
    op.add_column(
        "users",
        sa.Column("detail_views_today", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users",
        sa.Column("detail_views_reset_at", sa.Date(), nullable=True),
    )


def downgrade():
    op.drop_column("users", "detail_views_reset_at")
    op.drop_column("users", "detail_views_today")
