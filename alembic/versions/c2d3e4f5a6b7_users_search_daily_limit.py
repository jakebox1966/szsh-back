"""users 검색 일일 한도 컬럼 추가

회원(로그인) 검색 일일 한도 차감용. ai_calls_today/reset_at 과 동일 패턴.
비회원은 프론트 localStorage 가 담당하므로 컬럼 불필요.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-08 09:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("search_calls_today", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users",
        sa.Column("search_calls_reset_at", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "search_calls_reset_at")
    op.drop_column("users", "search_calls_today")
