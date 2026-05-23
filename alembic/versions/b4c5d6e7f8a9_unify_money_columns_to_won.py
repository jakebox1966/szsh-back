"""unify all monetary columns to BIGINT in 원

- 만원 → 원 (× 10,000):
  dart_emp_sttus.avg_annual_salary
  company_summary.avg_salary
  company_summary.avg_salary_official
  company_summary.prev_year_avg_salary
  salary_ranking.amount
  net_pay_reference.gross
  net_pay_reference.net

- 억원 → 원 (× 100,000,000):
  company_summary.revenue
  company_finance.revenue_current
  company_finance.operating_profit_current
  company_finance.net_profit_current
  company_finance_history.rev
  company_finance_history.profit

이미 원 단위인 컬럼(nps_monthly.notified_amount, dart_emp_sttus.annual_salary_total)은 손대지 않음.

Revision ID: b4c5d6e7f8a9
Revises: a3b1c2d4e5f6
Create Date: 2026-05-11
"""
from typing import Sequence, Union

from alembic import op


revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, None] = "a3b1c2d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, factor) — 만원 (factor=10000) 또는 억원 (factor=100000000)
MANWON_COLUMNS = [
    ("dart_emp_sttus", "avg_annual_salary"),
    ("company_summary", "avg_salary"),
    ("company_summary", "avg_salary_official"),
    ("company_summary", "prev_year_avg_salary"),
    ("salary_ranking", "amount"),
    ("net_pay_reference", "gross"),
    ("net_pay_reference", "net"),
]

EOK_COLUMNS = [
    ("company_summary", "revenue"),
    ("company_finance", "revenue_current"),
    ("company_finance", "operating_profit_current"),
    ("company_finance", "net_profit_current"),
    ("company_finance_history", "rev"),
    ("company_finance_history", "profit"),
]


def upgrade() -> None:
    # 만원 → 원: × 10,000 후 BIGINT 로 캐스팅
    for table, col in MANWON_COLUMNS:
        op.execute(
            f"ALTER TABLE {table} "
            f"ALTER COLUMN {col} TYPE BIGINT "
            f"USING (CASE WHEN {col} IS NULL THEN NULL ELSE ({col}::bigint * 10000) END)"
        )

    # 억원 → 원: × 100,000,000, 반올림 후 BIGINT (Numeric 소수점 절삭 방지)
    for table, col in EOK_COLUMNS:
        op.execute(
            f"ALTER TABLE {table} "
            f"ALTER COLUMN {col} TYPE BIGINT "
            f"USING (CASE WHEN {col} IS NULL THEN NULL ELSE ROUND({col} * 100000000)::bigint END)"
        )


def downgrade() -> None:
    # 원 → 만원: ÷ 10,000, INTEGER 로 환원 (소수점 절삭)
    for table, col in MANWON_COLUMNS:
        op.execute(
            f"ALTER TABLE {table} "
            f"ALTER COLUMN {col} TYPE INTEGER "
            f"USING (CASE WHEN {col} IS NULL THEN NULL ELSE ({col} / 10000)::integer END)"
        )

    # 원 → 억원: ÷ 100,000,000, Numeric(14,2) 로 환원
    for table, col in EOK_COLUMNS:
        op.execute(
            f"ALTER TABLE {table} "
            f"ALTER COLUMN {col} TYPE NUMERIC(14, 2) "
            f"USING (CASE WHEN {col} IS NULL THEN NULL ELSE ({col}::numeric / 100000000) END)"
        )
