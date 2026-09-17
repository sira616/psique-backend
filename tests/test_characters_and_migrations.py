import json
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy import create_engine

import app.models  # noqa: F401
from app.core.config import settings
from app.core.database import Base
from app.core.migrations import upgrade_to_head
from app.story.character_profile import CHARACTERS_DIR, parse_profile


def _lucia() -> dict:
    return json.loads((CHARACTERS_DIR / "lucia.json").read_text(encoding="utf-8"))


def test_los_perfiles_publicados_son_validos():
    for path in CHARACTERS_DIR.glob("*.json"):
        profile = parse_profile(path.read_bytes(), expected_id=path.stem)
        assert "Límites del personaje" in profile.render()


def test_un_campo_desconocido_rompe_al_cargar():
    data = _lucia() | {"contenido_explicito": True}
    with pytest.raises(ValidationError):
        parse_profile(json.dumps(data).encode())


def test_ningun_personaje_puede_ser_menor():
    with pytest.raises(ValidationError):
        parse_profile(json.dumps(_lucia() | {"edad": 17}).encode())


def test_un_personaje_sin_limites_no_carga():
    with pytest.raises(ValidationError):
        parse_profile(json.dumps(_lucia() | {"limites": []}).encode())


def test_el_id_debe_coincidir_con_el_fichero():
    with pytest.raises(ValueError):
        parse_profile((CHARACTERS_DIR / "lucia.json").read_bytes(), expected_id="mateo")


def test_las_migraciones_dejan_el_esquema_de_los_modelos(tmp_path: Path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'migrada.db').as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    upgrade_to_head()
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            diferencias = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    finally:
        engine.dispose()
    assert diferencias == [], f"Modelo cambiado sin migración: {diferencias}"
