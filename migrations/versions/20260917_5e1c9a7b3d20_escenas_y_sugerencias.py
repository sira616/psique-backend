"""escenas y sugerencias

Escena en curso y sus sugerencias por partida. Las partidas existentes quedan a null y
reciben las sugerencias de su fase hasta su siguiente turno.

Revision ID: 5e1c9a7b3d20
Revises: c4a8e2f71b90
Fecha: 2026-09-17 20:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '5e1c9a7b3d20'
down_revision: Union[str, None] = 'c4a8e2f71b90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('stories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scene_title', sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column('suggestions', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('suggestions_turn', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('stories', schema=None) as batch_op:
        batch_op.drop_column('suggestions_turn')
        batch_op.drop_column('suggestions')
        batch_op.drop_column('scene_title')
