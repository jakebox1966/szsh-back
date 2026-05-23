"""add subscribers column to company_hr_trends

월별 인원수 시계열 노출용. 라인 차트 그릴 때 사용.

Revision ID: f9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-05-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9b0c1d2e3f4"
down_revision: Union[str, None] = "f8a9b0c1d2e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "company_hr_trends",
        sa.Column("subscribers", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("company_hr_trends", "subscribers")
