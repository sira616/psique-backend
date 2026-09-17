"""perfiles e historias publicas

Revision ID: 91b541cfb075
Revises: b7c4d2e9f1a3
Fecha: 2026-09-17 00:10:20.967897
"""
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '91b541cfb075'
down_revision: Union[str, None] = 'b7c4d2e9f1a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _handle_base(username: str) -> str:
    # Copia congelada de `profile_service.handle_from_username`: una migración no importa
    # código de la app, que puede cambiar después y reescribir la historia.
    base = re.sub(r"[^a-z0-9_]", "_", username.lower())[:30]
    return base if len(base) >= 3 else (base + "___")[:3]


def upgrade() -> None:
    with op.batch_alter_table('story_blueprints', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_public', sa.Boolean(), server_default=sa.false(), nullable=False))
        batch_op.add_column(sa.Column('published_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('deleted_at', sa.DateTime(), nullable=True))
        batch_op.create_index('ix_story_blueprints_explore', ['is_public', 'published_at'], unique=False)

    # El handle entra nulable, se rellena y después pasa a NOT NULL + único: añadirlo NOT
    # NULL de golpe fallaría con cualquier cuenta ya existente.
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('handle', sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column('bio', sa.String(length=280), nullable=True))
        batch_op.add_column(sa.Column('link', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('avatar_path', sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column('banner_path', sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column('show_published', sa.Boolean(), server_default=sa.true(), nullable=False))
        batch_op.add_column(sa.Column('show_reading', sa.Boolean(), server_default=sa.true(), nullable=False))

    conn = op.get_bind()
    users = sa.table('users', sa.column('id', sa.String), sa.column('username', sa.String), sa.column('handle', sa.String))
    taken: set[str] = set()
    # Por fecha de alta: ante una colisión ("ana.b" y "ana-b"), la cuenta más antigua
    # conserva el handle limpio.
    rows = conn.execute(sa.text('SELECT id, username FROM users ORDER BY created_at, id')).fetchall()
    for user_id, username in rows:
        base = _handle_base(username)
        handle, n = base, 2
        while handle in taken:
            suffix = f"_{n}"
            handle = base[: 30 - len(suffix)] + suffix
            n += 1
        taken.add(handle)
        conn.execute(users.update().where(users.c.id == user_id).values(handle=handle))

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column('handle', existing_type=sa.String(length=30), nullable=False)
        batch_op.create_index(batch_op.f('ix_users_handle'), ['handle'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_handle'))
        batch_op.drop_column('show_reading')
        batch_op.drop_column('show_published')
        batch_op.drop_column('banner_path')
        batch_op.drop_column('avatar_path')
        batch_op.drop_column('link')
        batch_op.drop_column('bio')
        batch_op.drop_column('handle')

    with op.batch_alter_table('story_blueprints', schema=None) as batch_op:
        batch_op.drop_index('ix_story_blueprints_explore')
        batch_op.drop_column('deleted_at')
        batch_op.drop_column('published_at')
        batch_op.drop_column('is_public')
