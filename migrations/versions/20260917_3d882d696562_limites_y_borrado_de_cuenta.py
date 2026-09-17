"""límites y borrado de cuenta

Tope diario de turnos de chat por cuenta, rate limiter persistente (ventanas fijas) y
`story_blueprints.owner_id` nullable: al borrar una cuenta, sus historias que otras cuentas
siguen jugando quedan anónimas y borradas lógicamente. Tablas nuevas sin datos previos.

Revision ID: 3d882d696562
Revises: d2076038b9ae
Fecha: 2026-09-17 18:25:55.057361
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3d882d696562'
down_revision: Union[str, None] = 'd2076038b9ae'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('rate_limit_windows',
    sa.Column('key', sa.String(length=255), nullable=False),
    sa.Column('window_start', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('hits', sa.Integer(), server_default='0', nullable=False),
    sa.Column('expires_at', sa.BigInteger(), nullable=False),
    sa.PrimaryKeyConstraint('key', 'window_start')
    )
    with op.batch_alter_table('rate_limit_windows', schema=None) as batch_op:
        batch_op.create_index('ix_rate_limit_windows_expires_at', ['expires_at'], unique=False)

    op.create_table('chat_turn_usage',
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('turns', sa.Integer(), server_default='0', nullable=False),
    sa.CheckConstraint('turns >= 0', name='ck_chat_turn_usage_turns'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('user_id', 'day')
    )
    with op.batch_alter_table('story_blueprints', schema=None) as batch_op:
        batch_op.alter_column('owner_id',
               existing_type=sa.VARCHAR(length=36),
               nullable=True)



def downgrade() -> None:
    # Las huérfanas de cuentas borradas no caben en una columna NOT NULL.
    op.execute("DELETE FROM story_blueprints WHERE owner_id IS NULL")
    with op.batch_alter_table('story_blueprints', schema=None) as batch_op:
        batch_op.alter_column('owner_id',
               existing_type=sa.VARCHAR(length=36),
               nullable=False)

    op.drop_table('chat_turn_usage')
    with op.batch_alter_table('rate_limit_windows', schema=None) as batch_op:
        batch_op.drop_index('ix_rate_limit_windows_expires_at')

    op.drop_table('rate_limit_windows')
