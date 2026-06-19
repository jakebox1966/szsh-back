"""merge detail-view-quota + drop-nps-fkey

Revision ID: e541d03fb861
Revises: e4f5a6b7c8d9, f6a7b8c9d0e1
Create Date: 2026-06-20 03:27:43.486446

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e541d03fb861'
down_revision: Union[str, None] = ('e4f5a6b7c8d9', 'f6a7b8c9d0e1')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
