"""add public_id (nanoid) to users

외부 노출용 식별자. 내부 PK(id, 정수)는 그대로 두고 API/URL 에는 이 값만 노출.
기존 행은 backfill 로 각자 고유 nanoid 발급.

Revision ID: a1b2c3d4e5f6
Revises: f9b0c1d2e3f4
Create Date: 2026-06-02
"""
import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f9b0c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ALPHABET = (
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "_-"
)
_SIZE = 12


def _nanoid() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_SIZE))


def upgrade() -> None:
    # 1) 우선 nullable 로 추가 (기존 행이 있어도 실패 안 하도록)
    op.add_column("users", sa.Column("public_id", sa.String(length=12), nullable=True))

    # 2) 기존 유저 backfill — 배치 내 중복까지 방지
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id FROM users")).fetchall()
    seen: set[str] = set()
    for (uid,) in rows:
        while True:
            pid = _nanoid()
            if pid not in seen:
                seen.add(pid)
                break
        conn.execute(
            sa.text("UPDATE users SET public_id = :pid WHERE id = :id"),
            {"pid": pid, "id": uid},
        )

    # 3) NOT NULL + UNIQUE 인덱스 (모델의 unique=True, index=True 와 동일)
    op.alter_column("users", "public_id", nullable=False)
    op.create_index("ix_users_public_id", "users", ["public_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_public_id", table_name="users")
    op.drop_column("users", "public_id")
