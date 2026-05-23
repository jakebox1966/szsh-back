"""enrich raw DART tables — preserve all received fields

dart_raw: 기업개황 API 응답 필드 보강 (est_dt, corp_cls, ceo_nm, jurir_no,
          induty_code, acc_mt, ir_url, phn_no, fax_no, stock_name) +
          raw_json (JSONB) + fetched_at

dart_emp_sttus: 응답 필드 보강 (business_unit, regular_part_count,
                contract_part_count, avg_tenure_raw, remark, raw_json) +
                UNIQUE 제약을 business_unit 까지 포함

dart_finance_raw: 신규 테이블 — 재무정보 일괄다운로드 .txt 의 모든 항목코드
                  raw 보존. company_finance 는 이 테이블에서 파생.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-05-15 13:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── dart_raw 컬럼 추가 ──────────────────────────────────────────
    op.add_column("dart_raw", sa.Column("stock_name", sa.String(), nullable=True))
    op.add_column("dart_raw", sa.Column("jurir_no", sa.String(), nullable=True))
    op.add_column("dart_raw", sa.Column("ceo_nm", sa.String(), nullable=True))
    op.add_column("dart_raw", sa.Column("corp_cls", sa.String(length=1), nullable=True))
    op.add_column("dart_raw", sa.Column("ir_url", sa.String(), nullable=True))
    op.add_column("dart_raw", sa.Column("phn_no", sa.String(), nullable=True))
    op.add_column("dart_raw", sa.Column("fax_no", sa.String(), nullable=True))
    op.add_column("dart_raw", sa.Column("induty_code", sa.String(), nullable=True))
    op.add_column("dart_raw", sa.Column("est_dt", sa.Date(), nullable=True))
    op.add_column("dart_raw", sa.Column("acc_mt", sa.String(length=2), nullable=True))
    op.add_column("dart_raw", sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("dart_raw", sa.Column("fetched_at", sa.DateTime(), nullable=True))

    # ── dart_emp_sttus 컬럼 추가 + UNIQUE 재정의 ────────────────────
    op.add_column("dart_emp_sttus", sa.Column("business_unit", sa.String(), nullable=True))
    op.add_column("dart_emp_sttus", sa.Column("regular_part_count", sa.Integer(), nullable=True))
    op.add_column("dart_emp_sttus", sa.Column("contract_part_count", sa.Integer(), nullable=True))
    op.add_column("dart_emp_sttus", sa.Column("avg_tenure_raw", sa.String(), nullable=True))
    op.add_column("dart_emp_sttus", sa.Column("remark", sa.Text(), nullable=True))
    op.add_column("dart_emp_sttus", sa.Column("raw_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.drop_constraint("uq_emp_sttus", "dart_emp_sttus", type_="unique")
    op.create_unique_constraint(
        "uq_emp_sttus",
        "dart_emp_sttus",
        ["dart_id", "year", "sex", "business_unit"],
    )

    # ── dart_finance_raw 신규 테이블 ────────────────────────────────
    op.create_table(
        "dart_finance_raw",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dart_id", sa.Integer(), nullable=True),
        sa.Column("stock_code", sa.String(), nullable=True),
        sa.Column("bsns_year", sa.String(), nullable=False),
        sa.Column("report_type", sa.String(), nullable=True),
        sa.Column("statement_type", sa.String(), nullable=True),
        sa.Column("market_div", sa.String(), nullable=True),
        sa.Column("industry_code", sa.String(), nullable=True),
        sa.Column("industry_name", sa.String(), nullable=True),
        sa.Column("acc_mt", sa.String(length=2), nullable=True),
        sa.Column("end_dt", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("account_id", sa.String(), nullable=True),
        sa.Column("account_nm", sa.String(), nullable=True),
        sa.Column("current_amount", sa.BigInteger(), nullable=True),
        sa.Column("prev_amount", sa.BigInteger(), nullable=True),
        sa.Column("prev2_amount", sa.BigInteger(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["dart_id"], ["dart_raw.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stock_code", "bsns_year", "report_type", "statement_type", "account_id",
            name="uq_dart_finance_raw",
        ),
    )
    op.create_index("ix_dart_finance_raw_id", "dart_finance_raw", ["id"], unique=False)
    op.create_index("ix_dart_finance_raw_dart_id", "dart_finance_raw", ["dart_id"], unique=False)
    op.create_index("ix_dart_finance_raw_stock_code", "dart_finance_raw", ["stock_code"], unique=False)
    op.create_index("ix_dart_finance_raw_bsns_year", "dart_finance_raw", ["bsns_year"], unique=False)
    op.create_index("ix_dart_finance_raw_account_id", "dart_finance_raw", ["account_id"], unique=False)
    op.create_index(
        "idx_dart_finance_raw_lookup",
        "dart_finance_raw",
        ["stock_code", "bsns_year", "account_id"],
        unique=False,
    )


def downgrade() -> None:
    # ── dart_finance_raw 제거 ───────────────────────────────────────
    op.drop_index("idx_dart_finance_raw_lookup", table_name="dart_finance_raw")
    op.drop_index("ix_dart_finance_raw_account_id", table_name="dart_finance_raw")
    op.drop_index("ix_dart_finance_raw_bsns_year", table_name="dart_finance_raw")
    op.drop_index("ix_dart_finance_raw_stock_code", table_name="dart_finance_raw")
    op.drop_index("ix_dart_finance_raw_dart_id", table_name="dart_finance_raw")
    op.drop_index("ix_dart_finance_raw_id", table_name="dart_finance_raw")
    op.drop_table("dart_finance_raw")

    # ── dart_emp_sttus 원복 ─────────────────────────────────────────
    op.drop_constraint("uq_emp_sttus", "dart_emp_sttus", type_="unique")
    op.create_unique_constraint(
        "uq_emp_sttus",
        "dart_emp_sttus",
        ["dart_id", "year", "sex"],
    )
    op.drop_column("dart_emp_sttus", "raw_json")
    op.drop_column("dart_emp_sttus", "remark")
    op.drop_column("dart_emp_sttus", "avg_tenure_raw")
    op.drop_column("dart_emp_sttus", "contract_part_count")
    op.drop_column("dart_emp_sttus", "regular_part_count")
    op.drop_column("dart_emp_sttus", "business_unit")

    # ── dart_raw 원복 ───────────────────────────────────────────────
    op.drop_column("dart_raw", "fetched_at")
    op.drop_column("dart_raw", "raw_json")
    op.drop_column("dart_raw", "acc_mt")
    op.drop_column("dart_raw", "est_dt")
    op.drop_column("dart_raw", "induty_code")
    op.drop_column("dart_raw", "fax_no")
    op.drop_column("dart_raw", "phn_no")
    op.drop_column("dart_raw", "ir_url")
    op.drop_column("dart_raw", "corp_cls")
    op.drop_column("dart_raw", "ceo_nm")
    op.drop_column("dart_raw", "jurir_no")
    op.drop_column("dart_raw", "stock_name")
