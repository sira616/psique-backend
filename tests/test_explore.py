"""Historias públicas: Explorar, jugar las de otra cuenta, despublicar y borrar."""
import json

import pytest

from app.core.config import settings
from app.core.database import SessionLocal
from app.llm import router as llm_router
from app.models.story import StoryBlueprint
from tests.conftest import auth_headers, parse_sse
from tests.test_custom_stories import SECRETO, _definida, _perfil_llm

PREMISA = "Un farero solitario recibe cartas de alguien que no existe"


def _crear(client, headers, body):
    resp = client.post("/api/custom-stories", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _explorar(client, headers, **params) -> dict:
    resp = client.get("/api/explore", params={"limit": 50} | params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(pagina: dict) -> set[str]:
    return {c["id"] for c in pagina["items"]}


@pytest.fixture
def llm_concepto(monkeypatch, fake_llm):
    """Con clave: la primera llamada a `generate` crea el concepto; el resto es extracción."""
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "clave-falsa")
    respuestas = [json.dumps(_perfil_llm(), ensure_ascii=False)]
    extraccion = fake_llm.generate
    monkeypatch.setattr(
        llm_router, "generate",
        lambda sp, msgs, max_tokens=None: respuestas.pop(0) if respuestas else extraccion(sp, msgs, max_tokens=max_tokens),
    )
    return fake_llm


def test_explorar_muestra_solo_las_publicas_con_su_autor(client):
    autora = auth_headers(client)
    privada = _crear(client, autora, _definida())
    publica = _crear(client, autora, _definida(title="Café publicado", isPublic=True))
    assert privada["isPublic"] is False and publica["isPublic"] is True
    assert publica["publishedAt"] is not None

    pagina = _explorar(client, auth_headers(client))
    assert publica["id"] in _ids(pagina)
    assert privada["id"] not in _ids(pagina)

    tarjeta = next(c for c in pagina["items"] if c["id"] == publica["id"])
    yo = client.get("/api/me", headers=autora).json()
    assert tarjeta["author"] == {"displayName": yo["displayName"], "handle": yo["handle"], "avatarUrl": None}
    assert tarjeta["definition"]["name"] == "Carmen Ruiz"
    assert tarjeta["characterId"] == f"custom:{publica['id']}"
    assert tarjeta["isMine"] is False
    assert next(c for c in _explorar(client, autora)["items"] if c["id"] == publica["id"])["isMine"] is True


def test_explorar_no_filtra_premisa_ni_perfil_de_un_concepto(client, llm_concepto):
    autora = auth_headers(client)
    concepto = _crear(client, autora, {"mode": "concepto", "premise": PREMISA, "tone": "misterioso", "isPublic": True})

    resp = client.get("/api/explore", params={"mode": "concepto", "limit": 50}, headers=auth_headers(client))
    tarjeta = next(c for c in resp.json()["items"] if c["id"] == concepto["id"])
    assert set(tarjeta) == {"id", "characterId", "mode", "title", "hook", "tone", "definition", "author", "isMine", "publishedAt"}
    assert (tarjeta["title"], tarjeta["tone"], tarjeta["definition"]) == ("La carta del faro", "misterioso", None)
    perfil = _perfil_llm()["perfil"]
    for filtrado in (PREMISA, SECRETO, "Elio", perfil["mundo"], perfil["escenario_inicial"], perfil["saludo"]):
        assert filtrado not in resp.text


def test_filtro_de_modo_y_paginacion(client):
    autora = auth_headers(client)
    creadas = [_crear(client, autora, _definida(title=f"Café número {i}", isPublic=True))["id"] for i in range(3)]
    lectora = auth_headers(client)

    primera = _explorar(client, lectora, limit=2)
    # Recientes primero: las tres recién publicadas encabezan la lista.
    assert [c["id"] for c in primera["items"]] == creadas[::-1][:2]
    assert primera["nextOffset"] == 2
    segunda = _explorar(client, lectora, limit=2, offset=2)
    assert segunda["items"][0]["id"] == creadas[0]

    assert all(c["mode"] == "concepto" for c in _explorar(client, lectora, mode="concepto")["items"])
    assert client.get("/api/explore", params={"mode": "otro"}, headers=lectora).status_code == 422
    assert client.get("/api/explore", params={"limit": 51}, headers=lectora).status_code == 422
    assert client.get("/api/explore").status_code == 401


def test_publicar_despues_la_sube_arriba_y_solo_el_duenio_la_cambia(client):
    autora = auth_headers(client)
    historia = _crear(client, autora, _definida())
    _crear(client, autora, _definida(title="Otra ya publicada", isPublic=True))

    otra = auth_headers(client)
    assert client.patch(f"/api/custom-stories/{historia['id']}", json={"isPublic": True}, headers=otra).status_code == 404

    resp = client.patch(f"/api/custom-stories/{historia['id']}", json={"isPublic": True}, headers=autora)
    assert resp.status_code == 200 and resp.json()["isPublic"] is True
    assert _explorar(client, otra)["items"][0]["id"] == historia["id"]
    propias = {h["id"]: h for h in client.get("/api/custom-stories", headers=autora).json()}
    assert propias[historia["id"]]["isPublic"] is True

    assert client.patch(f"/api/custom-stories/{historia['id']}", json={}, headers=autora).status_code == 422
    assert client.patch(f"/api/custom-stories/{historia['id']}", json={"isPublic": True, "title": "x"}, headers=autora).status_code == 422


def test_jugar_publica_ajena_si_y_privada_ajena_no(client, fake_llm):
    autora = auth_headers(client)
    publica = _crear(client, autora, _definida(isPublic=True))
    privada = _crear(client, autora, _definida())
    jugadora = auth_headers(client)

    resp = client.post("/api/stories", json={"characterId": publica["characterId"]}, headers=jugadora)
    assert resp.status_code == 201, resp.text
    assert resp.json()["characterName"] == "Carmen Ruiz"
    assert client.post("/api/stories", json={"characterId": privada["characterId"]}, headers=jugadora).status_code == 404
    # Jugarla no la añade a sus personajes: eso sigue siendo lo propio.
    ids = {c["id"] for c in client.get("/api/characters", headers=jugadora).json()}
    assert publica["characterId"] not in ids


def _chat_ok(client, headers, story_id):
    resp = client.post(f"/api/stories/{story_id}/chat", json={"message": "Hola otra vez"}, headers=headers)
    assert resp.status_code == 200
    eventos = [n for n, _ in parse_sse(resp.text)]
    assert "error" not in eventos and eventos[-1] == "done"


def test_despublicar_la_saca_de_explorar_pero_la_partida_ajena_sigue(client, llm_concepto):
    autora = auth_headers(client)
    concepto = _crear(client, autora, {"mode": "concepto", "premise": PREMISA, "isPublic": True})
    jugadora = auth_headers(client)
    partida = client.post("/api/stories", json={"characterId": concepto["characterId"]}, headers=jugadora).json()

    client.patch(f"/api/custom-stories/{concepto['id']}", json={"isPublic": False}, headers=autora)
    assert concepto["id"] not in _ids(_explorar(client, jugadora))

    assert client.get(f"/api/stories/{partida['id']}", headers=jugadora).json()["characterName"] == "Elio Marín"
    _chat_ok(client, jugadora, partida["id"])
    # Una partida nueva ya no: vuelve a ser privada.
    assert client.post("/api/stories", json={"characterId": concepto["characterId"]}, headers=jugadora).status_code == 404


def test_borrar_la_saca_de_todo_pero_la_partida_ajena_sigue(client, fake_llm):
    autora = auth_headers(client)
    publica = _crear(client, autora, _definida(isPublic=True))
    jugadora = auth_headers(client)
    ajena = client.post("/api/stories", json={"characterId": publica["characterId"]}, headers=jugadora).json()
    propia = client.post("/api/stories", json={"characterId": publica["characterId"]}, headers=autora).json()

    assert client.delete(f"/api/custom-stories/{publica['id']}", headers=autora).status_code == 204

    assert publica["id"] not in _ids(_explorar(client, jugadora))
    assert client.get("/api/custom-stories", headers=autora).json() == []
    assert client.delete(f"/api/custom-stories/{publica['id']}", headers=autora).status_code == 404
    assert client.patch(f"/api/custom-stories/{publica['id']}", json={"isPublic": True}, headers=autora).status_code == 404
    # La del autor se va con la historia; la de la otra cuenta se puede seguir jugando.
    assert client.get(f"/api/stories/{propia['id']}", headers=autora).status_code == 404
    assert client.get(f"/api/stories/{ajena['id']}", headers=jugadora).json()["characterName"] == "Carmen Ruiz"
    _chat_ok(client, jugadora, ajena["id"])
    assert client.post("/api/stories", json={"characterId": publica["characterId"]}, headers=jugadora).status_code == 404

    with SessionLocal() as db:
        fila = db.get(StoryBlueprint, publica["id"])
        assert fila is not None and fila.deleted_at is not None and fila.is_public is False


def test_borrar_una_que_nadie_mas_jugo_la_borra_de_verdad(client, fake_llm):
    autora = auth_headers(client)
    publica = _crear(client, autora, _definida(isPublic=True))
    client.post("/api/stories", json={"characterId": publica["characterId"]}, headers=autora)
    assert client.delete(f"/api/custom-stories/{publica['id']}", headers=autora).status_code == 204
    with SessionLocal() as db:
        assert db.get(StoryBlueprint, publica["id"]) is None


def test_las_borradas_no_cuentan_para_el_limite(client, fake_llm, monkeypatch):
    from app.services import custom_story_service

    monkeypatch.setattr(custom_story_service, "MAX_BLUEPRINTS_PER_USER", 1)
    autora = auth_headers(client)
    primera = _crear(client, autora, _definida(isPublic=True))
    client.post("/api/stories", json={"characterId": primera["characterId"]}, headers=auth_headers(client))
    assert client.post("/api/custom-stories", json=_definida(), headers=autora).status_code == 409
    client.delete(f"/api/custom-stories/{primera['id']}", headers=autora)
    _crear(client, autora, _definida())
