"""resumen rodante

Marcador de hasta qué mensaje cubre `stories.summary`. Las partidas existentes quedan a
null (nada resumido) y se pliegan en su siguiente turno si ya tienen lote.

Revision ID: 78da2b41cc30
Revises: 5e1c9a7b3d20
Fecha: 2026-09-17 16:07:08.596120
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '78da2b41cc30'
down_revision: Union[str, None] = '5e1c9a7b3d20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('stories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('summary_upto_message_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('stories', schema=None) as batch_op:
        batch_op.drop_column('summary_upto_message_id')
