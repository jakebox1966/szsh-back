"""companies_nps_id_fkey 제거 — serving-only 배포 호환

서빙 API 는 companies.nps_id '값'만 사용(spotlight 의 IS NOT NULL 필터 등)하지
nps_raw 로의 FK 제약 자체는 불필요. nps_raw 를 운영에 보내지 않는 serving-only 덤프
복원 시 이 FK 가 위반을 일으키므로 제거(데이터 파이프라인은 로컬에서만 도므로 무결성 영향 없음).

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-06-20 02:45:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE companies DROP CONSTRAINT IF EXISTS companies_nps_id_fkey")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE companies ADD CONSTRAINT companies_nps_id_fkey "
        "FOREIGN KEY (nps_id) REFERENCES nps_raw(id)"
    )
