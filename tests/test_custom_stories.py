"""Historias propias: modos definida y concepto, con el LLM doblado."""
import json

import pytest

from app.core.config import settings
from app.llm import router as llm_router
from app.llm.prompts.story import REVEAL_GRADUALLY
from app.story.content_policy import EXPLICIT_INPUT_MESSAGE, MINORS_INPUT_MESSAGE, check_user_text
from tests.conftest import auth_headers, parse_sse

SECRETO = "En realidad es la heredera del faro y lo oculta desde hace años."


def _definida(**cambios) -> dict:
    return {
        "mode": "definida",
        "title": "Café a medianoche",
        "name": "Carmen Ruiz",
        "age": 34,
        "personality": "tímida, ingeniosa; leal",
        "speakingStyle": "Habla bajito y con frases cortas, pero suelta chistes secos.",
        "setting": "Una cafetería de guardia abierta toda la noche en Bilbao. Llueve fuera.",
        "tone": "melancólico y cálido",
        "backstory": "Dejó la arquitectura para abrir la cafetería de su padre y no se arrepiente casi nunca.",
    } | cambios


def _perfil_llm(**cambios) -> dict:
    perfil = {
        "nombre": "Elio Marín",
        "edad": 31,
        "tagline": "Farero en una isla donde el correo llega una vez al mes.",
        "personalidad": ["paciente", "curioso", "algo testarudo"],
        "forma_de_hablar": {"registro": "Pausado, con metáforas de mar.", "muletillas": [], "evita": []},
        "trasfondo": "Llegó a la isla huyendo de una ciudad que le quedaba grande y se quedó por el silencio.",
        "gustos": ["las tormentas"],
        "mundo": "Una isla atlántica con un solo faro, un bar y un barco de correo mensual.",
        "secretos": [SECRETO],
        "escenario_inicial": "El usuario desembarca del barco de correo con una carta dirigida al farero.",
        "saludo": "*Baja la escalera del faro.* ¿Traes carta o traes problemas?",
    }
    perfil.update(cambios)
    return {"titulo": "La carta del faro", "gancho": "Una carta sin remitente llega a la isla del faro.", "perfil": perfil}


@pytest.fixture
def llm_con_clave(monkeypatch):
    """Simula tener API key y devuelve, en orden, las respuestas configuradas."""
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "clave-falsa")
    calls: list[list] = []
    respuestas: list = []

    def generate(system_prompt, messages, *, max_tokens=None):
        calls.append(messages)
        respuesta = respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta

    monkeypatch.setattr(llm_router, "generate", generate)
    return respuestas, calls


def _crear(client, headers, body, esperado=201):
    resp = client.post("/api/custom-stories", json=body, headers=headers)
    assert resp.status_code == esperado, resp.text
    return resp.json()


# --- Modo definida ----------------------------------------------------------------------


def test_crear_definida_devuelve_lo_escrito_y_deriva_el_gancho(client):
    headers = auth_headers(client)
    body = _crear(client, headers, _definida())
    assert body["mode"] == "definida"
    assert body["characterId"] == f"custom:{body['id']}"
    assert body["hook"] == "Una cafetería de guardia abierta toda la noche en Bilbao."
    assert body["definition"]["name"] == "Carmen Ruiz"
    assert body["definition"]["personality"] == "tímida, ingeniosa, leal"
    assert body["definition"]["tone"] == "melancólico y cálido"
    assert body["premise"] is None


def test_definida_con_un_solo_rasgo_sigue_cumpliendo_el_esquema(client):
    body = _crear(client, auth_headers(client), _definida(personality="tímida", hook="Un gancho propio bien largo"))
    assert body["hook"] == "Un gancho propio bien largo"
    assert body["definition"]["personality"].startswith("tímida, ")


@pytest.mark.parametrize("edad", [17, 12, 91])
def test_edad_fuera_de_rango_es_422(client, edad):
    resp = client.post("/api/custom-stories", json=_definida(age=edad), headers=auth_headers(client))
    assert resp.status_code == 422
    assert "adultos" in resp.text


@pytest.mark.parametrize(
    "cambios",
    [
        {"title": "x"},
        {"backstory": "corto"},
        {"setting": "a" * 591},
        {"campo_inventado": "hola"},
        {"mode": "otro"},
    ],
)
def test_validacion_de_longitudes_y_campos(client, cambios):
    resp = client.post("/api/custom-stories", json=_definida(**cambios), headers=auth_headers(client))
    assert resp.status_code == 422


# --- Filtro de entrada ------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, mensaje",
    [
        (_definida(backstory="Trabaja en un club y busca sexo con cada cliente que entra por la puerta."), EXPLICIT_INPUT_MESSAGE),
        ({"mode": "concepto", "premise": "Una historia erótica en un tren nocturno por Europa"}, EXPLICIT_INPUT_MESSAGE),
        (_definida(personality="dulce, tiene 15 años"), MINORS_INPUT_MESSAGE),
        ({"mode": "concepto", "premise": "Romance entre dos adolescentes en el instituto"}, MINORS_INPUT_MESSAGE),
    ],
)
def test_entrada_explicita_o_con_menores_es_422(client, llm_con_clave, body, mensaje):
    _, calls = llm_con_clave
    resp = client.post("/api/custom-stories", json=body, headers=auth_headers(client))
    assert resp.status_code == 422
    assert resp.json()["detail"] == mensaje
    assert not calls  # se rechaza antes de gastar una llamada al modelo


def test_el_trasfondo_de_infancia_de_un_adulto_no_se_rechaza():
    assert check_user_text("De niña pasaba los veranos en el pueblo; con 16 años empezó a trabajar.") is None
    assert check_user_text("Conoció a una niña en el parque") == MINORS_INPUT_MESSAGE


# --- Modo concepto ----------------------------------------------------------------------


def test_concepto_valido_no_expone_el_perfil(client, llm_con_clave):
    respuestas, calls = llm_con_clave
    respuestas.append("Aquí lo tienes:\n" + json.dumps(_perfil_llm(), ensure_ascii=False))
    headers = auth_headers(client)

    resp = client.post(
        "/api/custom-stories",
        json={"mode": "concepto", "premise": "Un farero solitario recibe cartas de alguien que no existe", "tone": "misterioso"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "La carta del faro"
    assert body["definition"] is None
    assert body["tone"] == "misterioso"
    assert len(calls) == 1
    assert "<premisa>" in calls[0][0]["content"]
    for texto in (resp.text, client.get("/api/custom-stories", headers=headers).text, client.get("/api/characters", headers=headers).text):
        assert SECRETO not in texto
        assert "Elio" not in texto


def test_concepto_json_invalido_reintenta_y_acierta(client, llm_con_clave):
    respuestas, calls = llm_con_clave
    respuestas += ["no es json", json.dumps(_perfil_llm())]
    body = _crear(client, auth_headers(client), {"mode": "concepto", "premise": "Dos rivales de ajedrez atrapados en un hotel"})
    assert body["mode"] == "concepto"
    assert len(calls) == 2
    # El reintento lleva la respuesta mala y el motivo.
    assert [m["role"] for m in calls[1]] == ["user", "assistant", "user"]
    assert "no contiene un objeto JSON" in calls[1][-1]["content"]


@pytest.mark.parametrize(
    "mala",
    [
        json.dumps(_perfil_llm(edad=16)),
        json.dumps(_perfil_llm(secretos=[])),
        json.dumps(_perfil_llm(nombre="")),
        json.dumps(_perfil_llm(trasfondo="Tiene un pasado lleno de sexo y excesos que no sabe olvidar nunca.")),
        '{"titulo": "x"',
    ],
)
def test_concepto_doble_fallo_es_502(client, llm_con_clave, mala):
    respuestas, calls = llm_con_clave
    respuestas += [mala, mala]
    headers = auth_headers(client)
    resp = client.post("/api/custom-stories", json={"mode": "concepto", "premise": "Una librera y un cartógrafo perdido"}, headers=headers)
    assert resp.status_code == 502
    assert "reformular la premisa" in resp.json()["detail"]
    assert len(calls) == 2
    assert client.get("/api/custom-stories", headers=headers).json() == []


def test_concepto_con_llm_caido_es_503(client, llm_con_clave):
    respuestas, _ = llm_con_clave
    respuestas.append(llm_router.LLMUnavailableError("apagado"))
    resp = client.post("/api/custom-stories", json={"mode": "concepto", "premise": "Una librera y un cartógrafo perdido"}, headers=auth_headers(client))
    assert resp.status_code == 503


def test_concepto_en_modo_demo_no_llama_al_modelo(client, monkeypatch):
    def prohibido(*args, **kwargs):
        raise AssertionError("no debe llamar al LLM sin API key")

    monkeypatch.setattr(llm_router, "generate", prohibido)
    premisa = {"mode": "concepto", "premise": "Una restauradora de relojes y un viajero del tiempo que no lo sabe"}
    a = _crear(client, auth_headers(client), premisa)
    b = _crear(client, auth_headers(client), premisa)
    assert (a["title"], a["hook"]) == (b["title"], b["hook"])


# --- Listado, aislamiento y juego -------------------------------------------------------


def test_characters_mezcla_los_dos_origenes(client, monkeypatch):
    headers = auth_headers(client)
    definida = _crear(client, headers, _definida())
    concepto = _crear(client, headers, {"mode": "concepto", "premise": "Un farero y una cartera en una isla"})

    personajes = {c["id"]: c for c in client.get("/api/characters", headers=headers).json()}
    assert personajes["lucia"]["origin"] == "psique"
    assert personajes["lucia"]["name"] == "Lucía Ferrer" and personajes["lucia"]["mode"] is None

    propia = personajes[definida["characterId"]]
    assert (propia["origin"], propia["mode"], propia["name"], propia["age"]) == ("propia", "definida", "Carmen Ruiz", 34)

    oculta = personajes[concepto["characterId"]]
    assert (oculta["origin"], oculta["mode"]) == ("propia", "concepto")
    assert oculta["title"] and oculta["hook"]
    assert all(oculta[k] is None for k in ("name", "age", "tagline", "traits", "scenario"))

    # Otra cuenta solo ve los predefinidos.
    ajenos = client.get("/api/characters", headers=auth_headers(client)).json()
    assert {c["origin"] for c in ajenos} == {"psique"}


def test_aislamiento_entre_cuentas(client, fake_llm):
    duena = auth_headers(client)
    otra = auth_headers(client)
    propia = _crear(client, duena, _definida())

    assert client.get("/api/custom-stories", headers=otra).json() == []
    assert client.delete(f"/api/custom-stories/{propia['id']}", headers=otra).status_code == 404
    resp = client.post("/api/stories", json={"characterId": propia["characterId"]}, headers=otra)
    assert resp.status_code == 404
    assert len(client.get("/api/custom-stories", headers=duena).json()) == 1


def test_jugar_una_historia_propia_y_borrarla(client, fake_llm, monkeypatch):
    headers = auth_headers(client)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "clave-falsa")
    respuestas = [json.dumps(_perfil_llm())]
    extraccion = fake_llm.generate
    monkeypatch.setattr(
        llm_router, "generate",
        lambda sp, msgs, max_tokens=None: respuestas.pop(0) if respuestas else extraccion(sp, msgs, max_tokens=max_tokens),
    )
    concepto = _crear(client, headers, {"mode": "concepto", "premise": "Un farero recibe cartas de alguien que no existe"})

    story = client.post("/api/stories", json={"characterId": concepto["characterId"]}, headers=headers)
    assert story.status_code == 201, story.text
    story = story.json()
    assert story["characterId"] == concepto["characterId"]
    assert story["characterName"] == "Elio Marín"
    assert story["messages"][0]["content"].startswith("*Baja la escalera")

    resp = client.post(f"/api/stories/{story['id']}/chat", json={"message": "Traigo una carta."}, headers=headers)
    assert resp.status_code == 200
    assert [n for n, _ in parse_sse(resp.text)][-2:] == ["state", "done"]
    system_prompt = fake_llm.stream_calls[0][0]
    assert "Elio Marín" in system_prompt and SECRETO in system_prompt
    assert REVEAL_GRADUALLY in system_prompt

    assert client.delete(f"/api/custom-stories/{concepto['id']}", headers=headers).status_code == 204
    # Las historias jugadas con ese personaje se borran con él.
    assert client.get(f"/api/stories/{story['id']}", headers=headers).status_code == 404
    assert all(s["id"] != story["id"] for s in client.get("/api/stories", headers=headers).json())
    assert client.delete(f"/api/custom-stories/{concepto['id']}", headers=headers).status_code == 404


def test_un_personaje_predefinido_no_recibe_la_instruccion_de_revelar(client, fake_llm):
    headers = auth_headers(client)
    story = client.post("/api/stories", json={"characterId": "lucia"}, headers=headers).json()
    client.post(f"/api/stories/{story['id']}/chat", json={"message": "Hola"}, headers=headers)
    assert REVEAL_GRADUALLY not in fake_llm.stream_calls[0][0]


def test_sin_token_es_401(client):
    assert client.get("/api/custom-stories").status_code == 401
    assert client.post("/api/custom-stories", json=_definida()).status_code == 401
