"""drop salary_ranking table

글로벌 평균연봉 랭킹 테이블 제거.
사용처가 사실상 사라져 (메인 인사이트 카드/상세 페이지 빈 뱃지) 폐기.
산업군별 랭킹은 추후 별도 설계로 재도입 예정.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-05-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f("ix_salary_ranking_id"), table_name="salary_ranking")
    op.drop_table("salary_ranking")


def downgrade() -> None:
    op.create_table(
        "salary_ranking",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=True),
        sa.Column("change", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_salary_ranking_id"), "salary_ranking", ["id"], unique=False)
