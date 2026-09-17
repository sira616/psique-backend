"""Exportar y borrar la cuenta."""
from io import BytesIO
from pathlib import Path

from PIL import Image
from sqlalchemy import func, select

from app.core.database import SessionLocal
from app.models.auth import RefreshToken
from app.models.economy import OboloMovement, ScratchCard
from app.models.limits import ChatTurnUsage
from app.models.story import BookReview, ConductIncident, MemoryFact, Message, Story, StoryBlueprint, StoryEvent
from app.models.user import User
from tests.conftest import MEDIA_DIR, PASSWORD, register
from tests.test_custom_stories import _definida

PREMISA = "Un farero solitario recibe cartas de alguien que no existe"


def _cuenta(client):
    body = register(client)
    return body, {"Authorization": f"Bearer {body['access_token']}"}


def _crear(client, headers, body):
    resp = client.post("/api/custom-stories", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _partida(client, headers, character="lucia"):
    resp = client.post("/api/stories", json={"characterId": character}, headers=headers)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _chat(client, headers, story_id, message="Hola, ¿qué tal?"):
    return client.post(f"/api/stories/{story_id}/chat", json={"message": message}, headers=headers)


def _png() -> bytes:
    out = BytesIO()
    Image.new("RGB", (64, 64), "teal").save(out, format="PNG")
    return out.getvalue()


def _borrar(client, headers, password=PASSWORD, confirmation="BORRAR"):
    return client.request(
        "DELETE", "/api/me", json={"password": password, "confirmation": confirmation}, headers=headers
    )


def _cuenta_con_datos(client, fake_llm):
    body, headers = _cuenta(client)
    fake_llm.extraction = {"hechos": {"nombre": "Marta"}, "senales": ["cumplido"]}
    story_id = _partida(client, headers)
    assert _chat(client, headers, story_id, "Hola, me llamo Marta.").status_code == 200
    concepto = _crear(client, headers, {"mode": "concepto", "premise": PREMISA, "tone": "misterioso"})
    assert client.put("/api/books/lucia/reviews/me", json={"rating": 5, "text": "Precioso."}, headers=headers).status_code in (200, 201)
    assert client.post("/api/scratch-cards", headers=headers).status_code == 201
    assert client.post("/api/me/avatar", files={"file": ("a.png", _png(), "image/png")}, headers=headers).status_code == 200
    return body, headers, story_id, concepto


# --- Exportar -------------------------------------------------------------------------


def test_export_incluye_todo_y_nada_secreto(client, fake_llm):
    body, headers, story_id, concepto = _cuenta_con_datos(client, fake_llm)
    resp = client.get("/api/me/export", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-disposition"].startswith("attachment;")
    assert resp.headers["cache-control"] == "no-store"
    data = resp.json()

    assert data["format"] == "psique-export-1"
    assert data["account"]["username"] == body["user"]["username"]
    assert data["account"]["avatar_url"].startswith("/media/avatars/")
    texto = resp.text
    assert "password_hash" not in texto and "argon2" not in texto
    assert "jti" not in texto and body["access_token"] not in texto

    [historia] = data["customStories"]
    assert historia["id"] == concepto["id"] and historia["mode"] == "concepto"
    # El perfil generado es suyo: sí sale en la exportación.
    assert historia["profile"]["nombre"] and historia["premise"] == PREMISA

    [partida] = data["stories"]
    assert partida["id"] == story_id
    assert [m["role"] for m in partida["messages"]] == ["assistant", "user", "assistant"]
    assert {"key": "nombre", "value": "Marta"}.items() <= partida["facts"][0].items()
    assert any(e["kind"] == "turno" for e in partida["events"])

    assert data["reviews"][0]["rating"] == 5
    assert {m["reason"] for m in data["oboloMovements"]} == {"bienvenida"}
    # La tarjeta sin rascar no revela su número.
    assert "number" not in data["scratchCards"][0]
    assert data["chatUsage"][0]["turns"] == 1
    assert data["conductIncidents"] == []


def test_export_exige_sesion(client):
    assert client.get("/api/me/export").status_code == 401


# --- Borrar ---------------------------------------------------------------------------


def test_borrar_exige_contrasena_y_confirmacion(client):
    body, headers = _cuenta(client)
    resp = _borrar(client, headers, password="otra-contrasena-mala")
    assert resp.status_code == 403 and resp.json()["code"] == "invalid_password"
    assert _borrar(client, headers, confirmation="borrar").status_code == 422
    assert client.request("DELETE", "/api/me", json={"password": PASSWORD}, headers=headers).status_code == 422
    assert client.get("/api/me", headers=headers).status_code == 200


def test_borrar_elimina_todo_y_cierra_la_sesion(client, fake_llm):
    body, headers, story_id, concepto = _cuenta_con_datos(client, fake_llm)
    user_id = body["user"]["id"]
    with SessionLocal() as db:
        avatar = Path(MEDIA_DIR) / db.get(User, user_id).avatar_path
        db.add(ConductIncident(user_id=user_id, story_id=story_id, level="prohibido", rule="x"))
        db.commit()
    assert avatar.exists()

    resp = _borrar(client, headers)
    assert resp.status_code == 204, resp.text
    cookie = resp.headers["set-cookie"]
    assert "psique_refresh=" in cookie and "Path=/api/auth" in cookie and "Max-Age=0" in cookie

    assert not avatar.exists()
    assert client.get("/api/me", headers=headers).status_code == 401
    login = client.post("/api/auth/login", json={"login": body["user"]["username"], "password": PASSWORD})
    assert login.status_code == 401

    with SessionLocal() as db:
        assert db.get(User, user_id) is None
        assert db.get(StoryBlueprint, concepto["id"]) is None
        for model in (Story, BookReview, ScratchCard, OboloMovement, ChatTurnUsage, RefreshToken, ConductIncident):
            assert db.scalar(select(func.count()).select_from(model).where(model.user_id == user_id)) == 0
        for model in (Message, MemoryFact, StoryEvent):
            assert db.scalar(select(func.count()).select_from(model).where(model.story_id == story_id)) == 0


def test_borrar_autora_no_rompe_la_partida_de_otra_cuenta(client, fake_llm):
    autora, autora_h = _cuenta(client)
    publica = _crear(client, autora_h, _definida(title="La leen otras", isPublic=True))
    nadie = _crear(client, autora_h, _definida(title="No la lee nadie", isPublic=True))
    _, lectora_h = _cuenta(client)
    partida = _partida(client, lectora_h, publica["characterId"])
    assert _chat(client, lectora_h, partida).status_code == 200
    assert client.put(
        f"/api/books/{publica['characterId']}/reviews/me", json={"rating": 4}, headers=lectora_h
    ).status_code in (200, 201)

    assert _borrar(client, autora_h).status_code == 204

    with SessionLocal() as db:
        anonima = db.get(StoryBlueprint, publica["id"])
        assert anonima.owner_id is None and anonima.is_public is False and anonima.deleted_at is not None
        assert db.get(StoryBlueprint, nadie["id"]) is None

    # La lectora sigue su partida con el perfil, que ya no sale en Explorar ni se puede empezar.
    resp = _chat(client, lectora_h, partida, "¿Seguimos donde lo dejamos?")
    assert resp.status_code == 200 and "error" not in resp.text.split("event: done")[0]
    assert client.get(f"/api/stories/{partida}", headers=lectora_h).status_code == 200
    explorar = client.get("/api/explore", params={"limit": 50}, headers=lectora_h).json()
    assert publica["id"] not in {c["id"] for c in explorar["items"]}
    _, nueva_h = _cuenta(client)
    assert client.post("/api/stories", json={"characterId": publica["characterId"]}, headers=nueva_h).status_code == 404
    assert client.get("/api/stories", headers=lectora_h).status_code == 200
    perfil = client.get("/api/me/profile", headers=lectora_h)
    assert perfil.status_code == 200
