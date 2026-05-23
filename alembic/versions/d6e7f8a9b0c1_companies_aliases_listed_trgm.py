"""companies aliases + is_listed + pg_trgm 검색 인덱스

companies 보강:
  - aliases TEXT[]: 회사명 별칭 (NPS 같은 그룹 사업장명, DART eng_name/stock_name,
    한영 변환 자동 생성). UI fuzzy 검색용
  - is_listed BOOLEAN: 상장 여부 (DART corp_cls 기반)

pg_trgm extension + GIN 인덱스:
  - name trigram 검색 (오타·부분 일치 허용)
  - aliases 배열 contains 검색

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-05-16 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) pg_trgm extension (fuzzy 검색용 trigram 유사도)
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # 2) companies 컬럼 추가
    op.add_column(
        "companies",
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("is_listed", sa.Boolean(), nullable=True),
    )

    # 3) GIN 인덱스: name trigram (오타 허용 fuzzy 검색)
    op.execute(
        "CREATE INDEX idx_companies_name_trgm "
        "ON companies USING gin (name gin_trgm_ops)"
    )

    # 4) GIN 인덱스: aliases 배열 contains 검색
    op.execute(
        "CREATE INDEX idx_companies_aliases_gin "
        "ON companies USING gin (aliases)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_companies_aliases_gin")
    op.execute("DROP INDEX IF EXISTS idx_companies_name_trgm")
    op.drop_column("companies", "is_listed")
    op.drop_column("companies", "aliases")
    # pg_trgm extension 은 다른 테이블에서도 쓸 수 있어 downgrade 시 제거하지 않음
