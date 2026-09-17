"""historias propias

Revision ID: b7c4d2e9f1a3
Revises: 31243aab0559
Fecha: 2026-09-16 22:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c4d2e9f1a3'
down_revision: Union[str, None] = '31243aab0559'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('story_blueprints',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('owner_id', sa.String(length=36), nullable=False),
    sa.Column('mode', sa.String(length=12), nullable=False),
    sa.Column('title', sa.String(length=80), nullable=False),
    sa.Column('hook', sa.String(length=140), nullable=False),
    sa.Column('premise', sa.Text(), nullable=True),
    sa.Column('profile', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('story_blueprints', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_story_blueprints_owner_id'), ['owner_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('story_blueprints', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_story_blueprints_owner_id'))

    op.drop_table('story_blueprints')
