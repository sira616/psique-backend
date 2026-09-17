"""Herramientas de dev: solo cuentas is_dev y solo sobre sus propias partidas."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.story import ConductIncident, Story, StoryEvent
from app.models.user import User
from app.services import conduct_service
from app.story import state_machine as sm
from tests.conftest import register


def _cuenta(client, *, dev: bool):
    body = register(client)
    if dev:
        with SessionLocal() as db:
            db.get(User, body["user"]["id"]).is_dev = True
            db.commit()
    return body["user"], {"Authorization": f"Bearer {body['access_token']}"}


def _partida(client, headers, character="lucia"):
    resp = client.post("/api/stories", json={"characterId": character}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _saldo(client, headers):
    return client.get("/api/me/wallet", headers=headers).json()["balance"]


RUTAS = [
    ("post", "/api/dev/stories/{id}/phase", {"phase": "tension"}),
    ("post", "/api/dev/stories/{id}/affinity", {"delta": 5}),
    ("post", "/api/dev/stories/{id}/unlock-chapter", None),
    ("get", "/api/dev/stories/{id}/context", None),
    ("post", "/api/dev/stories/{id}/reopen", None),
]


@pytest.mark.parametrize("metodo, ruta, cuerpo", RUTAS)
def test_sin_is_dev_es_403_y_partida_ajena_es_404(client, metodo, ruta, cuerpo):
    _, normal = _cuenta(client, dev=False)
    story_id = _partida(client, normal)
    url = ruta.format(id=story_id)
    kwargs = {"json": cuerpo} if cuerpo is not None else {}

    assert getattr(client, metodo)(url, headers=normal, **kwargs).status_code == 403
    _, dev = _cuenta(client, dev=True)
    assert getattr(client, metodo)(url, headers=dev, **kwargs).status_code == 404
    assert getattr(client, metodo)(ruta.format(id="no-existe"), headers=dev, **kwargs).status_code == 404


def test_levantar_restriccion_exige_dev(client):
    _, normal = _cuenta(client, dev=False)
    assert client.post("/api/dev/me/lift-restriction", headers=normal).status_code == 403


def test_forzar_fase_y_ajustar_afinidad_sin_tocar_obolos(client):
    _, dev = _cuenta(client, dev=True)
    story_id = _partida(client, dev)
    saldo = _saldo(client, dev)

    resp = client.post(f"/api/dev/stories/{story_id}/phase", json={"phase": "conflicto"}, headers=dev)
    assert resp.status_code == 200 and resp.json()["phase"] == "conflicto"

    resp = client.post(f"/api/dev/stories/{story_id}/affinity", json={"value": 70}, headers=dev)
    assert resp.json()["affinity"] == 70
    resp = client.post(f"/api/dev/stories/{story_id}/affinity", json={"delta": -10}, headers=dev)
    assert resp.json()["affinity"] == 60
    assert client.post(f"/api/dev/stories/{story_id}/affinity", json={"delta": 1, "value": 2}, headers=dev).status_code == 422

    with SessionLocal() as db:
        eventos = db.scalars(select(StoryEvent).where(StoryEvent.story_id == story_id).order_by(StoryEvent.id)).all()
        assert [(e.kind, e.detail, e.weight) for e in eventos] == [
            ("transicion", "dev", None),
            ("ajuste", "dev", 70 - sm.BASE_AFFINITY),
            ("ajuste", "dev", -10),
        ]
    assert client.get(f"/api/stories/{story_id}", headers=dev).json()["state"]["affinity"] == 60
    assert _saldo(client, dev) == saldo


def test_desbloquear_capitulo_sin_pagar(client):
    _, dev = _cuenta(client, dev=True)
    story_id = _partida(client, dev)
    assert client.post(f"/api/dev/stories/{story_id}/unlock-chapter", headers=dev).status_code == 409
    with SessionLocal() as db:
        db.get(Story, story_id).pending_phase = "confianza"
        db.commit()
    saldo = _saldo(client, dev)
    resp = client.post(f"/api/dev/stories/{story_id}/unlock-chapter", headers=dev)
    assert resp.status_code == 200
    assert resp.json()["phase"] == "confianza" and resp.json()["chapter_locked"] is False
    assert _saldo(client, dev) == saldo


def test_ver_contexto_del_siguiente_turno(client):
    _, dev = _cuenta(client, dev=True)
    story_id = _partida(client, dev)
    body = client.get(f"/api/dev/stories/{story_id}/context", headers=dev).json()
    assert "Lucía" in body["systemPrompt"]
    assert body["window"][-1]["role"] == "assistant"
    assert body["summary"] == "" and body["summaryUptoMessageId"] is None
    assert body["sceneTitle"] and len(body["suggestions"]["items"]) == 3


def test_reabrir_partida_cerrada_y_levantar_restriccion(client, fake_llm):
    user, dev = _cuenta(client, dev=True)
    story_id = _partida(client, dev)
    resp = client.post(f"/api/stories/{story_id}/chat", json={"message": "me gustan los pitos"}, headers=dev)
    assert resp.json()["code"] == "story_closed"

    reabierta = client.post(f"/api/dev/stories/{story_id}/reopen", headers=dev)
    assert reabierta.status_code == 200 and reabierta.json()["status"] == "activa"
    assert client.post(f"/api/dev/stories/{story_id}/reopen", headers=dev).status_code == 409

    with SessionLocal() as db:
        db.get(User, user["id"]).restricted_until = conduct_service.now() + timedelta(days=settings.CONDUCT_RESTRICTION_DAYS)
        db.commit()
    assert client.get("/api/me", headers=dev).json()["restrictedUntil"] is not None
    resp = client.post("/api/dev/me/lift-restriction", headers=dev)
    assert resp.status_code == 200 and resp.json()["restrictedUntil"] is None
    with SessionLocal() as db:
        assert db.scalar(select(ConductIncident).where(ConductIncident.user_id == user["id"])) is None
