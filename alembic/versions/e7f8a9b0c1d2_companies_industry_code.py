"""companies.industry_code 컬럼 추가

분야(industry group) 그루핑용 KSIC 원본 코드 저장.
companies.industry 는 한국어 업종명(예: "증권 중개업")인데, 그룹핑 위해서는
KSIC 코드(prefix)가 필요. compute.py 가 NPS rep_wp.industry_code 또는 DART
induty_code 를 그대로 저장.

NPS 는 6자리 (KSIC 9차), DART 는 5자리 (10차). 자릿수로 차수 분기 가능.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-05-19 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("industry_code", sa.String(), nullable=True),
    )
    op.create_index(
        "idx_companies_industry_code",
        "companies",
        ["industry_code"],
    )


def downgrade() -> None:
    op.drop_index("idx_companies_industry_code", table_name="companies")
    op.drop_column("companies", "industry_code")
