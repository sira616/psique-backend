"""Entorno de Alembic.

Dos decisiones que evitan la clase de fallo que motivó todo esto:

- La URL sale de `settings.DATABASE_URL`, no de `alembic.ini`. Con dos sitios donde
  configurarla, tarde o temprano se migra una base y la app usa otra.
- `render_as_batch=True`. SQLite no sabe hacer casi ningún `ALTER TABLE`: sin esto,
  cualquier migración que cambie o borre una columna falla al aplicarse. El modo batch
  recrea la tabla por detrás y copia los datos.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.core.database import Base

# Importar el paquete entero registra todos los modelos en Base.metadata. Sin esto,
# autogenerate vería media base de datos y propondría borrar el resto.
import app.models  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            # Sin esto, cambiar un Integer por un String no se detecta y la migración
            # sale vacía.
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
