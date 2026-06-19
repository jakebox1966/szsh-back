"""company_id FK 인덱스 추가 (compute 멈춤 방지)

companies 행 삭제 시 자식 테이블의 company_id FK 체크가 인덱스 없이 풀스캔되어
compute 의 `DELETE FROM companies ...` 가 사실상 멈춤(특히 company_hr_trends 수백만 행).
5개 자식 테이블에 company_id 인덱스 추가. (company_summary/company_hr 는 unique 라 이미 인덱스 있음)

멱등(IF NOT EXISTS) — 로컬에 이미 수동 생성돼 있어도 안전.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-06-20 02:20:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES = [
    ("ix_company_finance_company_id", "company_finance"),
    ("ix_company_finance_history_company_id", "company_finance_history"),
    ("ix_company_hr_trends_company_id", "company_hr_trends"),
    ("ix_company_comments_company_id", "company_comments"),
    ("ix_employee_growth_company_id", "employee_growth"),
]


def upgrade() -> None:
    for name, table in _INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} (company_id)")


def downgrade() -> None:
    for name, _ in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
