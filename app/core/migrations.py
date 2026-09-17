"""Aplicar las migraciones pendientes al arrancar.

Antes se usaba `Base.metadata.create_all()`, que **crea tablas nuevas pero no toca las
que ya existen**. Añadir una columna a un modelo no la añadía a la base, y el fallo
aparecía mucho después: una ruta devolviendo 500 con todo el código correcto. Pasó tres
veces, la última renombrando `time_in_bed_minutes`.

Migrar en el arranque es una decisión de este despliegue, no una buena práctica
universal: aquí hay **un solo proceso** y una base de datos de pocos megas. Con varias
instancias arrancando a la vez habría que sacarlo a un paso aparte del despliegue,
porque dos procesos migrando en paralelo se pisan.
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.config import settings

# La raíz del repo: este fichero vive en app/core/.
ROOT = Path(__file__).resolve().parent.parent.parent


def alembic_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    # La URL manda desde settings, nunca desde el .ini: con dos fuentes se acaba
    # migrando una base mientras la app usa otra.
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return config


def upgrade_to_head() -> None:
    command.upgrade(alembic_config(), "head")


def stamp_head() -> None:
    """Marca la base como ya migrada, sin ejecutar nada.

    Para una base que ya existía antes de haber migraciones: sus tablas están puestas,
    pero Alembic no lo sabe y al primer `upgrade` intentaría crearlas otra vez.
    """
    command.stamp(alembic_config(), "head")
