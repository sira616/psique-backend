"""apelaciones y términos

Incidentes de conducta con extracto para revisión, apelación y resolución auditada;
aceptación de términos y privacidad en las cuentas. Todo nullable: los incidentes
existentes quedan sin extracto ni apelación y las cuentas existentes sin términos
aceptados (el cliente se los pedirá al entrar).

Revision ID: a61f0c2d9e47
Revises: 3d882d696562
Fecha: 2026-09-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a61f0c2d9e47'
down_revision: Union[str, None] = '3d882d696562'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('conduct_incidents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('excerpt', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('appealed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('appeal_text', sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column('review_status', sa.String(length=12), nullable=True))
        batch_op.add_column(sa.Column('reviewed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('reviewed_by_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('reviewed_by_handle', sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column('review_note', sa.String(length=300), nullable=True))
        batch_op.create_index(batch_op.f('ix_conduct_incidents_review_status'), ['review_status'], unique=False)

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('terms_accepted_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('terms_version', sa.String(length=20), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('terms_version')
        batch_op.drop_column('terms_accepted_at')

    with op.batch_alter_table('conduct_incidents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_conduct_incidents_review_status'))
        batch_op.drop_column('review_note')
        batch_op.drop_column('reviewed_by_handle')
        batch_op.drop_column('reviewed_by_id')
        batch_op.drop_column('reviewed_at')
        batch_op.drop_column('review_status')
        batch_op.drop_column('appeal_text')
        batch_op.drop_column('appealed_at')
        batch_op.drop_column('excerpt')
