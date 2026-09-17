"""libros partidas y resenas

Una partida activa por (cuenta, libro), primera lectura gratis configurable y reseñas.

Las cuentas que ya tenían varias partidas del mismo libro conservan activa la más reciente
(por `updated_at`, luego `created_at`, luego `id`); el resto pasa a su historial como
archivada. Tiene que ir antes del índice único parcial, que fallaría con duplicados.

Revision ID: c4a8e2f71b90
Revises: 7d3307cbd054
Fecha: 2026-09-17 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4a8e2f71b90'
down_revision: Union[str, None] = '7d3307cbd054'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# SQL portable (SQLite y Postgres) y congelado aquí: una migración no importa código de la
# app. Archiva cada partida para la que exista otra del mismo (usuario, libro) posterior.
ARCHIVAR_DUPLICADAS = """
UPDATE stories SET status = 'archivada', archived_at = updated_at
WHERE status = 'activa' AND EXISTS (
    SELECT 1 FROM stories AS otra
    WHERE otra.user_id = stories.user_id
      AND otra.character_id = stories.character_id
      AND otra.id <> stories.id
      AND (
          otra.updated_at > stories.updated_at
          OR (otra.updated_at = stories.updated_at AND otra.created_at > stories.created_at)
          OR (otra.updated_at = stories.updated_at AND otra.created_at = stories.created_at AND otra.id > stories.id)
      )
)
"""


def archivar_duplicadas(conn) -> None:
    conn.execute(sa.text(ARCHIVAR_DUPLICADAS))


def upgrade() -> None:
    op.create_table('book_reviews',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.String(length=36), nullable=False),
    sa.Column('book_id', sa.String(length=40), nullable=False),
    sa.Column('rating', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.CheckConstraint('rating BETWEEN 1 AND 5', name='ck_book_reviews_rating'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'book_id', name='uq_book_reviews_user_book')
    )
    with op.batch_alter_table('book_reviews', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_book_reviews_book_id'), ['book_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_book_reviews_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('stories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('status', sa.String(length=12), server_default='activa', nullable=False))
        batch_op.add_column(sa.Column('archived_at', sa.DateTime(), nullable=True))

    with op.batch_alter_table('story_blueprints', schema=None) as batch_op:
        batch_op.add_column(sa.Column('free_first_read', sa.Boolean(), server_default=sa.true(), nullable=False))

    archivar_duplicadas(op.get_bind())

    # Fuera del batch: en SQLite el batch recrearía la tabla sin necesidad.
    op.create_index(
        'uq_stories_activa',
        'stories',
        ['user_id', 'character_id'],
        unique=True,
        sqlite_where=sa.text("status = 'activa'"),
        postgresql_where=sa.text("status = 'activa'"),
    )


def downgrade() -> None:
    op.drop_index('uq_stories_activa', table_name='stories')

    with op.batch_alter_table('story_blueprints', schema=None) as batch_op:
        batch_op.drop_column('free_first_read')

    # Las archivadas vuelven a ser partidas normales: el esquema anterior no las distingue.
    with op.batch_alter_table('stories', schema=None) as batch_op:
        batch_op.drop_column('archived_at')
        batch_op.drop_column('status')

    with op.batch_alter_table('book_reviews', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_book_reviews_user_id'))
        batch_op.drop_index(batch_op.f('ix_book_reviews_book_id'))

    op.drop_table('book_reviews')
