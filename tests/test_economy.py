"""Óbolos, rasca y gana y capítulos de pago: el servidor decide cada importe."""
import secrets
from datetime import date
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.migrations import alembic_config
from app.models.economy import ScratchCard
from app.models.story import Story
from app.models.user import User
from app.services import economy_service, scratch_service
from tests.conftest import register

HOY = date(2026, 9, 17)


def _cuenta(client) -> tuple[dict, dict]:
    sesion = register(client)
    return sesion["user"], {"Authorization": f"Bearer {sesion['access_token']}"}


@pytest.fixture
def hoy(monkeypatch):
    dia = {"valor": HOY}
    monkeypatch.setattr(scratch_service, "local_today", lambda: dia["valor"])
    return dia


def _numero(monkeypatch, numero: int):
    monkeypatch.setattr(secrets, "randbelow", lambda n: numero - 1)


def _rascar(client, headers) -> dict:
    resp = client.post("/api/scratch-cards", headers=headers)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _revelar(client, headers, card_id):
    return client.post(f"/api/scratch-cards/{card_id}/reveal", headers=headers)


# --- Monedero ---------------------------------------------------------------------


def test_registrarse_da_la_bienvenida(client):
    _, headers = _cuenta(client)
    wallet = client.get("/api/me/wallet", headers=headers).json()
    assert wallet["balance"] == settings.WELCOME_OBOLOS
    assert [(m["amount"], m["reason"], m["reference"]) for m in wallet["movements"]] == [
        (settings.WELCOME_OBOLOS, "bienvenida", None)
    ]


def test_dos_gastos_con_saldo_para_uno_solo_cobran_una_vez(client):
    user, _ = _cuenta(client)
    with SessionLocal() as a, SessionLocal() as b:
        # B ya leyó saldo suficiente antes de que A gaste: el UPDATE condicional no se fía
        # de esa lectura.
        assert b.get(User, user["id"]).obolos == settings.WELCOME_OBOLOS
        economy_service.spend(a, user["id"], settings.WELCOME_OBOLOS, "capitulo")
        a.commit()
        with pytest.raises(economy_service.InsufficientObolosError):
            economy_service.spend(b, user["id"], settings.WELCOME_OBOLOS, "capitulo")
        b.rollback()
        assert economy_service.balance(b, user["id"]) == 0


def test_ajuste_dev_exige_cuenta_dev(client):
    user, headers = _cuenta(client)
    assert client.post("/api/dev/obolos", json={"amount": 10}, headers=headers).status_code == 403

    with SessionLocal() as db:
        db.get(User, user["id"]).is_dev = True
        db.commit()
    resp = client.post("/api/dev/obolos", json={"amount": 10}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["balance"] == settings.WELCOME_OBOLOS + 10
    assert resp.json()["movements"][0]["reason"] == "ajuste_dev"
    for fuera_de_rango in (0, 1001):
        assert client.post("/api/dev/obolos", json={"amount": fuera_de_rango}, headers=headers).status_code == 422


# --- Rasca y gana -----------------------------------------------------------------


def test_el_numero_no_sale_hasta_revelar(client, hoy):
    _, headers = _cuenta(client)
    resp = client.post("/api/scratch-cards", headers=headers)
    assert resp.status_code == 201
    assert set(resp.json()) == {"id", "created_at", "remaining"}
    assert resp.json()["remaining"] == settings.SCRATCH_DAILY_CARDS - 1

    today = client.get("/api/scratch-cards/today", headers=headers).json()
    assert today["pending_card"] == {"id": resp.json()["id"], "created_at": resp.json()["created_at"]}
    assert "number" not in resp.json() and "number" not in today
    with SessionLocal() as db:
        assert 1 <= db.get(ScratchCard, resp.json()["id"]).number <= 10


def test_pedir_otra_sin_rascar_devuelve_la_pendiente(client, hoy):
    _, headers = _cuenta(client)
    primera = client.post("/api/scratch-cards", headers=headers)
    segunda = client.post("/api/scratch-cards", headers=headers)
    assert (primera.status_code, segunda.status_code) == (201, 200)
    assert segunda.json() == primera.json()


def test_limite_diario_y_cambio_de_dia(client, hoy, monkeypatch):
    _numero(monkeypatch, 1)
    _, headers = _cuenta(client)
    for _ in range(settings.SCRATCH_DAILY_CARDS):
        assert _revelar(client, headers, _rascar(client, headers)["id"]).status_code == 200

    resp = client.post("/api/scratch-cards", headers=headers)
    assert resp.status_code == 409
    assert resp.json() == {"detail": "Ya has rascado todas las tarjetas de hoy. Vuelve mañana."}
    today = client.get("/api/scratch-cards/today", headers=headers).json()
    assert (today["remaining"], today["daily_limit"], today["pending_card"]) == (0, settings.SCRATCH_DAILY_CARDS, None)

    hoy["valor"] = date(2026, 9, 18)
    resp = client.post("/api/scratch-cards", headers=headers)
    assert resp.status_code == 201
    assert resp.json()["remaining"] == settings.SCRATCH_DAILY_CARDS - 1


def test_revelar_premia_una_sola_vez(client, hoy, monkeypatch):
    _numero(monkeypatch, settings.SCRATCH_WINNING_NUMBER)
    _, headers = _cuenta(client)
    card = _rascar(client, headers)

    primera = _revelar(client, headers, card["id"])
    assert primera.status_code == 200
    assert primera.json() == {
        "id": card["id"],
        "number": settings.SCRATCH_WINNING_NUMBER,
        "won": True,
        "prize": settings.SCRATCH_PRIZE,
        "balance": settings.WELCOME_OBOLOS + settings.SCRATCH_PRIZE,
        "remaining": settings.SCRATCH_DAILY_CARDS - 1,
    }
    assert _revelar(client, headers, card["id"]).json() == primera.json()

    wallet = client.get("/api/me/wallet", headers=headers).json()
    assert wallet["balance"] == settings.WELCOME_OBOLOS + settings.SCRATCH_PRIZE
    assert [m["reference"] for m in wallet["movements"] if m["reason"] == "rasca"] == [f"scratch:{card['id']}"]


def test_sin_el_numero_ganador_no_hay_premio(client, hoy, monkeypatch):
    _numero(monkeypatch, 1)
    _, headers = _cuenta(client)
    body = _revelar(client, headers, _rascar(client, headers)["id"]).json()
    assert (body["number"], body["won"], body["prize"], body["balance"]) == (1, False, 0, settings.WELCOME_OBOLOS)


def test_tarjeta_ajena_o_inexistente_es_404(client, hoy):
    _, headers = _cuenta(client)
    _, otros = _cuenta(client)
    card = _rascar(client, headers)
    assert _revelar(client, otros, card["id"]).status_code == 404
    assert _revelar(client, headers, 999_999_999).status_code == 404


# --- Capítulos --------------------------------------------------------------------


def _historia_bloqueada(client, headers) -> str:
    story = client.post("/api/stories", json={"characterId": "lucia"}, headers=headers).json()
    assert (story["state"]["chapter_locked"], story["state"]["next_phase"]) == (False, None)
    assert story["state"]["chapter_cost"] == settings.CHAPTER_COST
    with SessionLocal() as db:
        db.get(Story, story["id"]).pending_phase = "confianza"
        db.commit()
    return story["id"]


def test_desbloquear_cobra_y_avanza(client):
    _, headers = _cuenta(client)
    story_id = _historia_bloqueada(client, headers)
    state = client.get(f"/api/stories/{story_id}", headers=headers).json()["state"]
    assert (state["phase"], state["chapter_locked"], state["next_phase"]) == ("conocerse", True, "confianza")

    resp = client.post(f"/api/stories/{story_id}/unlock-chapter", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["phase"], body["chapter_locked"], body["next_phase"]) == ("confianza", False, None)
    assert body["transition"]["to"] == "confianza"
    assert body["balance"] == settings.WELCOME_OBOLOS - settings.CHAPTER_COST

    movimiento = client.get("/api/me/wallet", headers=headers).json()["movements"][0]
    assert (movimiento["amount"], movimiento["reason"], movimiento["reference"]) == (
        -settings.CHAPTER_COST, "capitulo", f"story:{story_id}:confianza"
    )
    assert client.post(f"/api/stories/{story_id}/unlock-chapter", headers=headers).status_code == 409


def test_desbloquear_sin_saldo_es_402_y_no_toca_nada(client):
    user, headers = _cuenta(client)
    story_id = _historia_bloqueada(client, headers)
    with SessionLocal() as db:
        economy_service.spend(db, user["id"], settings.WELCOME_OBOLOS, "ajuste_dev")
        db.commit()

    resp = client.post(f"/api/stories/{story_id}/unlock-chapter", headers=headers)
    assert resp.status_code == 402
    assert resp.json() == {
        "detail": "Te faltan óbolos para desbloquear este capítulo. Puedes ganar más en Rasca y gana."
    }
    state = client.get(f"/api/stories/{story_id}", headers=headers).json()["state"]
    assert (state["phase"], state["next_phase"]) == ("conocerse", "confianza")
    assert client.get("/api/me/wallet", headers=headers).json()["balance"] == 0


def test_desbloquear_historia_ajena_es_404(client):
    _, headers = _cuenta(client)
    _, otros = _cuenta(client)
    story_id = _historia_bloqueada(client, headers)
    assert client.post(f"/api/stories/{story_id}/unlock-chapter", headers=otros).status_code == 404


# --- Migración --------------------------------------------------------------------


def test_la_migracion_da_la_bienvenida_a_las_cuentas_existentes(tmp_path: Path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'antigua.db').as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = alembic_config()
    command.upgrade(config, "91b541cfb075")
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, username, password_hash, display_name, handle) "
                    "VALUES ('u1', 'antigua', 'x', 'Antigua', 'antigua')"
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT obolos FROM users WHERE id = 'u1'")) == 5
            assert conn.execute(text("SELECT amount, reason FROM obolo_movements")).all() == [(5, "bienvenida")]
    finally:
        engine.dispose()
