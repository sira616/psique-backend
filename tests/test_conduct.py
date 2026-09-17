"""Política de contenido aplicada: cierre de partidas, reconducción en +18, restricción de
cuenta y acceso a libros +18."""
from datetime import timedelta

from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.story import ConductIncident, Message, Story, StoryEvent
from app.models.user import User
from app.services import conduct_service
from tests.conftest import parse_sse, register

EXPLICITO = "me gustan los pitos, enséñamelos"
PROHIBIDO = "te voy a violar"


def _cuenta(client, *, adulta=False):
    body = register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    if adulta:
        assert client.post("/api/me/adult-confirmation", json={"confirm": True}, headers=headers).status_code == 200
    return body["user"], headers


def _libro_adulto(client, headers) -> str:
    payload = {
        "mode": "definida",
        "title": "Noches de hotel",
        "name": "Irene Sol",
        "age": 36,
        "personality": "directa, irónica; cálida",
        "speakingStyle": "Frases cortas, mucho doble sentido y ninguna prisa.",
        "setting": "El bar de un hotel de Lisboa a las dos de la madrugada.",
        "tone": "sensual y elegante",
        "backstory": "Traductora que vive de hotel en hotel y no se queda nunca más de una semana.",
        "adult": True,
    }
    resp = client.post("/api/custom-stories", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["adult"] is True
    return resp.json()["characterId"]


def _partida(client, headers, character="lucia"):
    resp = client.post("/api/stories", json={"characterId": character}, headers=headers)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def _chat(client, headers, story_id, message):
    return client.post(f"/api/stories/{story_id}/chat", json={"message": message}, headers=headers)


def _estado_bd(story_id):
    with SessionLocal() as db:
        story = db.get(Story, story_id)
        mensajes = db.scalars(select(Message.content).where(Message.story_id == story_id)).all()
        eventos = db.scalars(select(StoryEvent.kind).where(StoryEvent.story_id == story_id)).all()
        return story, list(mensajes), list(eventos)


def test_explicito_en_libro_para_todos_cierra_la_partida_sin_llamar_al_llm(client, fake_llm):
    _, headers = _cuenta(client)
    story = _partida(client, headers)

    resp = _chat(client, headers, story["id"], EXPLICITO)

    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "story_closed" and body["closedAt"] and body["restrictedUntil"] is None
    assert fake_llm.stream_calls == [] and fake_llm.generate_calls == []
    guardada, mensajes, eventos = _estado_bd(story["id"])
    assert guardada.status == "cerrada" and guardada.closed_reason
    assert EXPLICITO not in mensajes and "pitos" not in guardada.closed_reason
    assert eventos == [] and guardada.affinity == story["state"]["affinity"]

    # Todo lo posterior: 403 story_closed, y se sigue leyendo en solo lectura.
    assert _chat(client, headers, story["id"], "hola").json()["code"] == "story_closed"
    assert client.post(f"/api/stories/{story['id']}/unlock-chapter", headers=headers).json()["code"] == "story_closed"
    leida = client.get(f"/api/stories/{story['id']}", headers=headers).json()
    assert leida["status"] == "cerrada" and leida["state"]["quickChoices"] == []
    historial = client.get("/api/books/lucia/history", headers=headers).json()
    assert [(h["storyId"], h["status"]) for h in historial] == [(story["id"], "cerrada")]


def test_sensual_pasa_y_prohibido_cierra_tambien_en_mas_18(client, fake_llm):
    _, headers = _cuenta(client, adulta=True)
    libro = _libro_adulto(client, headers)
    story = _partida(client, headers, libro)

    ok = _chat(client, headers, story["id"], "Te beso despacio y acaricio tu cuello")
    assert ok.status_code == 200 and parse_sse(ok.text)[-1][0] == "done"

    resp = _chat(client, headers, story["id"], PROHIBIDO)
    assert resp.status_code == 403 and resp.json()["code"] == "story_closed"


def test_explicito_en_mas_18_reconduce_sin_cerrar_ni_puntuar(client, fake_llm):
    _, headers = _cuenta(client, adulta=True)
    story = _partida(client, headers, _libro_adulto(client, headers))

    resp = _chat(client, headers, story["id"], EXPLICITO)

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "content_redirected" and "Irene Sol" in body["reply"]
    assert fake_llm.stream_calls == []
    guardada, mensajes, eventos = _estado_bd(story["id"])
    assert guardada.status == "activa" and EXPLICITO not in mensajes and eventos == []
    with SessionLocal() as db:
        assert db.scalar(select(ConductIncident).where(ConductIncident.story_id == story["id"])) is None


def test_la_sugerencia_tambien_pasa_por_el_clasificador(client, fake_llm):
    _, headers = _cuenta(client)
    story = _partida(client, headers)
    with SessionLocal() as db:
        guardada = db.get(Story, story["id"])
        guardada.suggestions = {
            "origin": "modelo",
            "items": [
                {"id": "s1", "intent": "humor", "label": "Broma", "message": EXPLICITO},
                {"id": "s2", "intent": "preguntar", "label": "Preguntar", "message": "¿Qué tal el día?"},
                {"id": "s3", "intent": "escuchar", "label": "Escuchar", "message": "Te escucho."},
            ],
        }
        db.commit()
    resp = client.post(f"/api/stories/{story['id']}/chat", json={"choiceId": "s1"}, headers=headers)
    assert resp.status_code == 403 and resp.json()["code"] == "story_closed"


def test_tres_cierres_en_30_dias_restringen_la_cuenta(client, fake_llm):
    user, headers = _cuenta(client)
    for i, libro in enumerate(("lucia", "mateo", "lucia")):
        story = _partida(client, headers, libro)
        if i == 2:
            # Releer tras un cierre está permitido: el POST crea una nueva.
            assert story["status"] == "activa"
        resp = _chat(client, headers, story["id"], EXPLICITO)
        assert resp.json()["code"] == "story_closed"
    until = resp.json()["restrictedUntil"]
    assert until is not None

    me = client.get("/api/me", headers=headers).json()
    assert me["restrictedUntil"] == until
    for peticion in (
        lambda: client.post("/api/stories", json={"characterId": "mateo"}, headers=headers),
        lambda: client.post("/api/books/lucia/reread", headers=headers),
    ):
        bloqueada = peticion()
        assert bloqueada.status_code == 403 and bloqueada.json()["code"] == "account_restricted"
        assert bloqueada.json()["restrictedUntil"] == until
    # Leer sigue funcionando.
    assert client.get("/api/books/lucia/history", headers=headers).status_code == 200


def test_incidentes_fuera_de_la_ventana_no_cuentan(client, fake_llm):
    user, headers = _cuenta(client)
    vieja = _partida(client, headers, "mateo")
    with SessionLocal() as db:
        antes = conduct_service.now() - timedelta(days=settings.CONDUCT_WINDOW_DAYS + 1)
        for _ in range(settings.CONDUCT_CLOSURES_LIMIT - 1):
            db.add(ConductIncident(user_id=user["id"], story_id=vieja["id"], level="explicito", created_at=antes))
        db.commit()
    story = _partida(client, headers)
    assert _chat(client, headers, story["id"], EXPLICITO).json()["restrictedUntil"] is None


def test_restriccion_bloquea_continuar_una_partida_activa(client, fake_llm):
    user, headers = _cuenta(client)
    story = _partida(client, headers)
    with SessionLocal() as db:
        db.get(User, user["id"]).restricted_until = conduct_service.now() + timedelta(days=1)
        db.commit()
    for resp in (
        _chat(client, headers, story["id"], "hola"),
        client.post("/api/stories", json={"characterId": "lucia"}, headers=headers),
    ):
        assert resp.status_code == 403 and resp.json()["code"] == "account_restricted"
    assert fake_llm.stream_calls == []


def test_libro_mas_18_exige_confirmar_mayoria_de_edad(client, fake_llm):
    _, autora = _cuenta(client, adulta=True)
    libro = _libro_adulto(client, autora)
    client.patch(f"/api/custom-stories/{libro.split(':')[1]}", json={"isPublic": True}, headers=autora)

    _, headers = _cuenta(client)
    pagina = client.get(f"/api/books/{libro}", headers=headers).json()
    assert pagina["adult"] is True and pagina["viewer"]["adultRequired"] is True
    explorar = client.get("/api/explore", params={"limit": 50}, headers=headers).json()["items"]
    assert libro not in {c["characterId"] for c in explorar}

    resp = client.post("/api/stories", json={"characterId": libro}, headers=headers)
    assert resp.status_code == 403 and resp.json()["code"] == "adult_required"

    confirmada = client.post("/api/me/adult-confirmation", json={"confirm": True}, headers=headers).json()
    assert confirmada["adultConfirmed"] is True
    assert client.post("/api/me/adult-confirmation", json={"confirm": False}, headers=headers).status_code == 422
    explorar = client.get("/api/explore", params={"limit": 50}, headers=headers).json()["items"]
    tarjeta = next(c for c in explorar if c["characterId"] == libro)
    assert tarjeta["adult"] is True
    story = _partida(client, headers, libro)

    # Revocar la confirmación corta también la partida empezada.
    assert client.delete("/api/me/adult-confirmation", headers=headers).json()["adultConfirmed"] is False
    assert _chat(client, headers, story["id"], "hola").json()["code"] == "adult_required"


def test_opcion_mas_18_solo_la_edita_el_autor(client):
    _, autora = _cuenta(client)
    libro = _libro_adulto(client, autora)
    blueprint_id = libro.split(":")[1]
    resp = client.patch(f"/api/custom-stories/{blueprint_id}", json={"adult": False}, headers=autora)
    assert resp.status_code == 200 and resp.json()["adult"] is False
    _, otra = _cuenta(client)
    assert client.patch(f"/api/custom-stories/{blueprint_id}", json={"adult": True}, headers=otra).status_code == 404


def test_predefinidos_llevan_marca_mas_18(client):
    _, headers = _cuenta(client)
    personajes = client.get("/api/characters", headers=headers).json()
    assert {c["id"]: c["adult"] for c in personajes if c["origin"] == "psique"} == {"lucia": False, "mateo": False}
