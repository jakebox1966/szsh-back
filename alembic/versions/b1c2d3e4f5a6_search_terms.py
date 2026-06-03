"""add search_terms (글로벌 최신 검색어)

전체 사용자 검색어를 keyword 단위로 upsert. 최신순(last_searched_at) 노출.

Revision ID: b1c2d3e4f5a6
Revises: a1b2c3d4e5f6
Create Date: 2026-06-03
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "search_terms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("keyword", sa.String(), nullable=False),
        sa.Column("search_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_searched_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_search_terms_keyword", "search_terms", ["keyword"], unique=True)
    op.create_index("ix_search_terms_last_searched_at", "search_terms", ["last_searched_at"])


def downgrade() -> None:
    op.drop_index("ix_search_terms_last_searched_at", table_name="search_terms")
    op.drop_index("ix_search_terms_keyword", table_name="search_terms")
    op.drop_table("search_terms")
