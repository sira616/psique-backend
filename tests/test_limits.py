"""Tope diario de turnos, rate limiter persistente y script de limpieza."""
import os
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.rate_limit import RateLimiter, delete_expired
from app.llm import router as llm_router
from app.models.auth import RefreshToken
from app.models.limits import ChatTurnUsage, RateLimitWindow
from app.models.story import Story, StoryBlueprint
from app.models.user import User
from app.services import usage_service
from scripts import cleanup
from tests.conftest import parse_sse, register
from tests.test_custom_stories import _definida


def _cuenta(client, *, dev=False):
    body = register(client)
    if dev:
        with SessionLocal() as db:
            db.get(User, body["user"]["id"]).is_dev = True
            db.commit()
    return body["user"], {"Authorization": f"Bearer {body['access_token']}"}


def _partida(client, headers, character="lucia"):
    resp = client.post("/api/stories", json={"characterId": character}, headers=headers)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _chat(client, headers, story_id, message="Hola, ¿qué tal el día?"):
    return client.post(f"/api/stories/{story_id}/chat", json={"message": message}, headers=headers)


def _uso(client, headers):
    resp = client.get("/api/me/usage", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- Tope diario de turnos ------------------------------------------------------------


def test_uso_inicial_y_reset_a_medianoche_de_madrid(client):
    _, headers = _cuenta(client)
    uso = _uso(client, headers)
    assert uso["turnsUsed"] == 0 and uso["unlimited"] is False
    assert uso["turnsLimit"] == settings.CHAT_TURNS_PER_DAY == uso["turnsRemaining"]
    resets = datetime.fromisoformat(uso["resetsAt"])
    assert resets.utcoffset() in (timedelta(hours=1), timedelta(hours=2))
    assert (resets.hour, resets.minute) == (0, 0)
    assert resets.date() == usage_service.local_today() + timedelta(days=1)


def test_al_llegar_al_tope_429_antes_del_llm(client, fake_llm, monkeypatch):
    monkeypatch.setattr(settings, "CHAT_TURNS_PER_DAY", 2)
    _, headers = _cuenta(client)
    story_id = _partida(client, headers)

    for _ in range(2):
        assert _chat(client, headers, story_id).status_code == 200
    llamadas = len(fake_llm.stream_calls)

    resp = _chat(client, headers, story_id)
    assert resp.status_code == 429
    body = resp.json()
    assert body["code"] == "daily_turn_limit" and body["turnsLimit"] == 2
    assert datetime.fromisoformat(body["resetsAt"]) == usage_service.resets_at()
    assert len(fake_llm.stream_calls) == llamadas
    # El mensaje rechazado no se guarda ni cuenta un turno.
    story = client.get(f"/api/stories/{story_id}", headers=headers).json()
    assert [m["role"] for m in story["messages"]].count("user") == 2
    assert _uso(client, headers) | {"resetsAt": None} == {
        "turnsUsed": 2, "turnsLimit": 2, "turnsRemaining": 0, "unlimited": False, "resetsAt": None,
    }


def test_rechazos_de_politica_no_gastan_cupo(client, fake_llm, monkeypatch):
    monkeypatch.setattr(settings, "CHAT_TURNS_PER_DAY", 1)
    _, headers = _cuenta(client)
    story_id = _partida(client, headers)
    assert _chat(client, headers, story_id, "te voy a violar").status_code == 403
    assert _uso(client, headers)["turnsUsed"] == 0


def test_el_cupo_es_por_dia_y_por_cuenta(client, fake_llm, monkeypatch):
    monkeypatch.setattr(settings, "CHAT_TURNS_PER_DAY", 1)
    user, headers = _cuenta(client)
    story_id = _partida(client, headers)
    with SessionLocal() as db:
        # Lleno ayer: no cuenta hoy.
        db.add(ChatTurnUsage(user_id=user["id"], day=usage_service.local_today() - timedelta(days=1), turns=1))
        db.commit()
    assert _chat(client, headers, story_id).status_code == 200
    assert _chat(client, headers, story_id).status_code == 429
    _, otra = _cuenta(client)
    assert _chat(client, otra, _partida(client, otra)).status_code == 200


def test_modelo_caido_devuelve_el_turno(client, fake_llm, monkeypatch):
    monkeypatch.setattr(settings, "CHAT_TURNS_PER_DAY", 1)
    _, headers = _cuenta(client)
    story_id = _partida(client, headers)
    fake_llm.stream_error = llm_router.LLMUnavailableError("caído")
    eventos = [n for n, _ in parse_sse(_chat(client, headers, story_id).text)]
    assert "error" in eventos
    assert _uso(client, headers)["turnsUsed"] == 0
    fake_llm.stream_error = None
    assert _chat(client, headers, story_id).status_code == 200


def test_dev_sin_tope_pero_contado(client, fake_llm, monkeypatch):
    monkeypatch.setattr(settings, "CHAT_TURNS_PER_DAY", 1)
    _, headers = _cuenta(client, dev=True)
    story_id = _partida(client, headers)
    for _ in range(3):
        assert _chat(client, headers, story_id).status_code == 200
    uso = _uso(client, headers)
    assert uso == uso | {"turnsUsed": 3, "turnsLimit": None, "turnsRemaining": None, "unlimited": True}

    monkeypatch.setattr(settings, "DEV_UNLIMITED_TURNS", False)
    assert _chat(client, headers, story_id).status_code == 429


def test_reserva_condicionada_no_pasa_del_tope(client, monkeypatch):
    monkeypatch.setattr(settings, "CHAT_TURNS_PER_DAY", 3)
    user, _ = _cuenta(client)
    with SessionLocal() as db:
        cuenta = db.get(User, user["id"])
        resultados = [usage_service.reserve_turn(db, cuenta) for _ in range(5)]
        assert resultados == [True, True, True, False, False]
        assert usage_service.turns_used(db, user["id"]) == 3


# --- Rate limiter persistente ---------------------------------------------------------


class Reloj:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_rate_limiter_ventana_fija_y_sobrevive_a_un_reinicio():
    reloj = Reloj(1_000_020.0)
    clave = f"/prueba:{uuid.uuid4().hex}"
    limiter = RateLimiter(2, 60, time_func=reloj)
    assert [limiter.hit(clave) for _ in range(3)] == [True, True, False]

    # "Reinicio": otra instancia con la misma configuración lee el mismo contador.
    assert RateLimiter(2, 60, time_func=reloj).hit(clave) is False
    # Otra clave no comparte cupo.
    assert limiter.hit(clave + "-otra") is True

    # Ventana fija: al empezar la siguiente se vuelve a poder.
    reloj.t = (reloj.t // 60 + 1) * 60
    assert limiter.hit(clave) is True


def test_rate_limiter_responde_429(client, monkeypatch):
    from fastapi import FastAPI, Depends
    from fastapi.testclient import TestClient

    app = FastAPI()
    limiter = RateLimiter(1, 60)

    @app.get(f"/limitada-{uuid.uuid4().hex}", dependencies=[Depends(limiter)])
    def limitada():
        return {"ok": True}

    ruta = app.routes[-1].path
    with TestClient(app) as c:
        assert c.get(ruta).status_code == 200
        resp = c.get(ruta)
        assert resp.status_code == 429 and "Demasiados intentos" in resp.json()["detail"]


def test_rate_limiter_limpia_ventanas_caducadas():
    clave = f"/limpieza:{uuid.uuid4().hex}"
    reloj = Reloj(2_000_000.0)
    RateLimiter(5, 60, time_func=reloj).hit(clave)
    with SessionLocal() as db:
        filas = lambda: db.scalar(select(func.count()).select_from(RateLimitWindow).where(RateLimitWindow.key == clave))
        assert filas() == 1
        delete_expired(db, reloj.t + 30)
        db.commit()
        assert filas() == 1
        delete_expired(db, reloj.t + 120)
        db.commit()
        assert filas() == 0


# --- Script de limpieza ---------------------------------------------------------------


@pytest.fixture
def media_tmp(tmp_path):
    root = tmp_path / "media"
    for sub in ("avatars", "banners"):
        (root / sub).mkdir(parents=True)
    return root


def _viejo(path: Path, contenido=b"x"):
    path.write_bytes(contenido)
    hace_dos_horas = time.time() - 7200
    os.utime(path, (hace_dos_horas, hace_dos_horas))
    return path


def test_cleanup_dry_run_no_borra_y_apply_si(client, fake_llm, media_tmp):
    autora, autora_h = _cuenta(client)
    _, lectora_h = _cuenta(client)

    def crear(**extra):
        resp = client.post("/api/custom-stories", json=_definida(**extra), headers=autora_h)
        assert resp.status_code == 201, resp.text
        return resp.json()

    sola = crear(title="Nadie la leyó")
    leida = crear(title="Leída por otra", isPublic=True)
    _partida(client, lectora_h, leida["characterId"])
    for h in (sola, leida):
        assert client.delete(f"/api/custom-stories/{h['id']}", headers=autora_h).status_code == 204
    with SessionLocal() as db:
        # Borrada lógicamente pero ya sin partidas (p. ej. la lectora borró su cuenta).
        huerfana = StoryBlueprint(
            id=uuid.uuid4().hex, owner_id=None, mode="definida", title="Huérfana", hook="x",
            profile=db.get(StoryBlueprint, leida["id"]).profile, deleted_at=datetime(2026, 1, 1),
        )
        db.add(huerfana)
        con_avatar = db.get(User, autora["id"])
        con_avatar.avatar_path = "avatars/en-uso.webp"
        db.add(RefreshToken(jti=str(uuid.uuid4()), user_id=autora["id"], expires_at=datetime(2020, 1, 1)))
        db.commit()
        huerfana_id = huerfana.id

    _viejo(media_tmp / "avatars" / "en-uso.webp")
    _viejo(media_tmp / "banners" / "huerfano.webp")
    (media_tmp / "avatars" / "recien-subido.webp").write_bytes(b"x")
    _viejo(media_tmp / "ajeno.txt")

    with SessionLocal() as db:
        informe = cleanup.run(db, media_tmp, apply=False)
    assert any(huerfana_id in b for b in informe.blueprints)
    assert not any(leida["id"] in b for b in informe.blueprints)
    assert informe.media_files == ["banners/huerfano.webp"]
    assert informe.refresh_tokens >= 1
    assert "Se borraría" in "\n".join(informe.lines(False))
    with SessionLocal() as db:
        assert db.get(StoryBlueprint, huerfana_id) is not None
    assert (media_tmp / "banners" / "huerfano.webp").exists()

    with SessionLocal() as db:
        cleanup.run(db, media_tmp, apply=True)
    with SessionLocal() as db:
        assert db.get(StoryBlueprint, huerfana_id) is None
        # La que otra cuenta sigue leyendo no se toca.
        assert db.get(StoryBlueprint, leida["id"]) is not None
        assert db.scalar(select(func.count()).select_from(RefreshToken).where(RefreshToken.expires_at < datetime(2021, 1, 1))) == 0
    assert not (media_tmp / "banners" / "huerfano.webp").exists()
    assert (media_tmp / "avatars" / "en-uso.webp").exists()
    assert (media_tmp / "avatars" / "recien-subido.webp").exists()
    assert (media_tmp / "ajeno.txt").exists()


def test_cleanup_cli_es_dry_run_por_defecto(capsys):
    assert cleanup.main([]) == 0
    salida = capsys.readouterr().out
    assert "Dry-run" in salida and "Se borraría" in salida
