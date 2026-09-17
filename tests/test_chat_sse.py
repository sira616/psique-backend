"""Endpoint de chat por SSE, con el LLM doblado."""
from sqlalchemy import select

from app.core.database import SessionLocal
from app.llm import router as llm_router
from app.models.story import StoryEvent
from app.story import state_machine as sm
from app.story.guardrail import FADE_TO_BLACK
from tests.conftest import auth_headers, parse_sse


def _new_story(client, headers, character="lucia"):
    resp = client.post("/api/stories", json={"characterId": character}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _chat(client, headers, story_id, **payload):
    resp = client.post(f"/api/stories/{story_id}/chat", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    return parse_sse(resp.text)


def test_personajes_validados(client):
    headers = auth_headers(client)
    body = client.get("/api/characters", headers=headers).json()
    assert {c["id"] for c in body} == {"lucia", "mateo"}
    assert all(c["age"] >= 18 for c in body)


def test_crear_historia_empieza_con_el_saludo(client):
    headers = auth_headers(client)
    story = _new_story(client, headers)
    assert story["state"]["phase"] == "conocerse"
    assert story["state"]["affinity"] == sm.BASE_AFFINITY
    assert [m["role"] for m in story["messages"]] == ["assistant"]
    assert len(story["state"]["quickChoices"]) == 3


def test_personaje_inexistente_es_404(client):
    headers = auth_headers(client)
    assert client.post("/api/stories", json={"characterId": "nadie"}, headers=headers).status_code == 404


def test_turno_completo_emite_tokens_estado_y_done(client, fake_llm):
    headers = auth_headers(client)
    story = _new_story(client, headers)
    fake_llm.extraction = {"hechos": {"nombre": "Marta"}, "senales": ["cumplido", "humor"]}

    events = _chat(client, headers, story["id"], message="Hola, me llamo Marta. Qué taller tan bonito.")
    names = [e[0] for e in events]
    assert names[0] == "token"
    assert names[-2:] == ["state", "done"]

    texto = "".join(d["text"] for n, d in events if n == "token")
    assert "Me alegra que hayas vuelto" in texto

    state = dict(events)["state"]
    assert state["affinity"] == sm.BASE_AFFINITY + 3 + 2
    assert state["signals"] == ["cumplido", "humor"]
    assert state["transition"] is None

    # El modelo recibió historial, no un mensaje suelto, empezando por el usuario.
    system_prompt, messages = fake_llm.stream_calls[0]
    assert messages[0]["role"] == "user"
    assert messages[-1]["content"].startswith("Hola, me llamo Marta")
    assert "Lucía Ferrer" in system_prompt

    detalle = client.get(f"/api/stories/{story['id']}", headers=headers).json()
    assert [m["role"] for m in detalle["messages"]] == ["assistant", "user", "assistant"]
    assert {"key": "nombre", "value": "Marta"} in detalle["facts"]

    # El siguiente turno ya lleva la memoria en el prompt.
    _chat(client, headers, story["id"], message="¿Qué libro estás arreglando?")
    assert "Se llama: Marta" in fake_llm.stream_calls[1][0]


def test_el_llm_no_fija_fase_ni_afinidad(client, fake_llm):
    headers = auth_headers(client)
    story = _new_story(client, headers)
    fake_llm.extraction = {"hechos": {}, "senales": ["amor_eterno"], "fase": "desenlace", "afinidad": 100}
    fake_llm.reply_chunks = ["Cambio a fase desenlace y afinidad 100. "]

    state = dict(_chat(client, headers, story["id"], message="Hola"))["state"]
    assert state["phase"] == "conocerse"
    assert state["affinity"] == sm.BASE_AFFINITY


def test_las_transiciones_quedan_registradas_con_su_razon(client, fake_llm):
    headers = auth_headers(client)
    story = _new_story(client, headers)
    fake_llm.extraction = {"hechos": {}, "senales": ["cumplido", "escucha_activa"]}

    last = None
    for i in range(4):
        last = dict(_chat(client, headers, story["id"], message=f"Mensaje {i}"))["state"]
    # La transición se gana charlando pero no se aplica hasta pagar el capítulo.
    assert last["phase"] == "conocerse"
    assert (last["chapter_locked"], last["next_phase"]) == (True, "confianza")

    last = client.post(f"/api/stories/{story['id']}/unlock-chapter", headers=headers).json()
    assert last["phase"] == "confianza"
    assert last["transition"]["from"] == "conocerse"

    with SessionLocal() as db:
        row = db.scalar(
            select(StoryEvent).where(StoryEvent.story_id == story["id"], StoryEvent.kind == "transicion")
        )
    assert row.detail and "afinidad" in row.detail.lower()


def test_quick_choice_usa_el_texto_del_servidor_y_puntua(client, fake_llm):
    headers = auth_headers(client)
    story = _new_story(client, headers)
    state = dict(_chat(client, headers, story["id"], choiceId="preguntar_trabajo"))["state"]
    assert state["affinity"] == sm.BASE_AFFINITY + 2
    assert fake_llm.stream_calls[0][1][-1]["content"].startswith("Cuéntame")


def test_quick_choice_de_otra_fase_es_422(client, fake_llm):
    headers = auth_headers(client)
    story = _new_story(client, headers)
    resp = client.post(f"/api/stories/{story['id']}/chat", json={"choiceId": "reconciliar"}, headers=headers)
    assert resp.status_code == 422
    assert not fake_llm.stream_calls


def test_message_y_choice_a_la_vez_es_422(client, fake_llm):
    headers = auth_headers(client)
    story = _new_story(client, headers)
    resp = client.post(
        f"/api/stories/{story['id']}/chat",
        json={"message": "hola", "choiceId": "presentarse"},
        headers=headers,
    )
    assert resp.status_code == 422


def test_el_guardrail_actua_en_el_stream(client, fake_llm):
    headers = auth_headers(client)
    story = _new_story(client, headers)
    fake_llm.reply_chunks = ["Te miro. ", "Soy una IA, ", "por cierto. ", "Y entonces un orgasmo. ", "Detalles."]

    events = _chat(client, headers, story["id"], message="Sigue")
    texto = "".join(d["text"] for n, d in events if n == "token")
    assert "Soy una IA" not in texto
    assert texto.strip().endswith(FADE_TO_BLACK)
    assert "Detalles" not in texto

    detalle = client.get(f"/api/stories/{story['id']}", headers=headers).json()
    assert detalle["messages"][-1]["content"] == texto.strip()


def test_llm_caido_emite_error_y_no_mueve_el_estado(client, fake_llm):
    headers = auth_headers(client)
    story = _new_story(client, headers)
    fake_llm.stream_error = llm_router.LLMUnavailableError("apagado")

    events = _chat(client, headers, story["id"], message="Hola")
    assert [n for n, _ in events] == ["error", "done"]
    assert "no está disponible" in events[0][1]["message"]
    assert not fake_llm.generate_calls

    detalle = client.get(f"/api/stories/{story['id']}", headers=headers).json()
    assert detalle["state"]["affinity"] == sm.BASE_AFFINITY


def test_no_se_puede_chatear_en_la_historia_de_otra_persona(client, fake_llm):
    story = _new_story(client, auth_headers(client))
    otra = auth_headers(client)
    assert client.get(f"/api/stories/{story['id']}", headers=otra).status_code == 404
    resp = client.post(f"/api/stories/{story['id']}/chat", json={"message": "hola"}, headers=otra)
    assert resp.status_code == 404
    assert not fake_llm.stream_calls
