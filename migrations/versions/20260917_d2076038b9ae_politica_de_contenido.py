"""política de contenido

Libros +18, confirmación de mayoría de edad, cierre de partidas, incidentes de conducta con
restricción temporal de la cuenta y peso explícito en los eventos de ajuste (dev). Todo
nullable o con default: las filas existentes quedan como libros para todos los públicos,
cuentas sin confirmar ni restringir y partidas sin cerrar.

Revision ID: d2076038b9ae
Revises: 78da2b41cc30
Fecha: 2026-09-17 16:38:16.528302
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2076038b9ae'
down_revision: Union[str, None] = '78da2b41cc30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('conduct_incidents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('story_id', sa.String(length=36), nullable=False),
    sa.Column('level', sa.String(length=12), nullable=False),
    sa.Column('rule', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['story_id'], ['stories.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('conduct_incidents', schema=None) as batch_op:
        batch_op.create_index('ix_conduct_incidents_user_fecha', ['user_id', 'created_at'], unique=False)

    with op.batch_alter_table('stories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('closed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('closed_reason', sa.String(length=120), nullable=True))

    with op.batch_alter_table('story_blueprints', schema=None) as batch_op:
        batch_op.add_column(sa.Column('adult', sa.Boolean(), server_default=sa.false(), nullable=False))

    with op.batch_alter_table('story_events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('weight', sa.Integer(), nullable=True))

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('adult_confirmed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('restricted_until', sa.DateTime(), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('restricted_until')
        batch_op.drop_column('adult_confirmed_at')

    with op.batch_alter_table('story_events', schema=None) as batch_op:
        batch_op.drop_column('weight')

    with op.batch_alter_table('story_blueprints', schema=None) as batch_op:
        batch_op.drop_column('adult')

    with op.batch_alter_table('stories', schema=None) as batch_op:
        batch_op.drop_column('closed_reason')
        batch_op.drop_column('closed_at')

    with op.batch_alter_table('conduct_incidents', schema=None) as batch_op:
        batch_op.drop_index('ix_conduct_incidents_user_fecha')

    op.drop_table('conduct_incidents')
