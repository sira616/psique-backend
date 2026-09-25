"""portada y descripción de historias propias

Descripción larga del autor y ruta de la portada en `story_blueprints`. Las dos nullable:
las historias que ya existen se quedan sin descripción y sin portada, y el frontend sabe
enseñar una tarjeta sin imagen.

Revision ID: 4f7c1b9d2a63
Revises: a61f0c2d9e47
Fecha: 2026-09-24
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '4f7c1b9d2a63'
down_revision: Union[str, None] = 'a61f0c2d9e47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('story_blueprints', schema=None) as batch_op:
        batch_op.add_column(sa.Column('description', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('cover_path', sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('story_blueprints', schema=None) as batch_op:
        batch_op.drop_column('cover_path')
        batch_op.drop_column('description')
