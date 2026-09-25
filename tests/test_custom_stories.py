"""Historias propias: modos definida y concepto, con el LLM doblado."""
import json
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.llm import router as llm_router
from app.llm.prompts.story import REVEAL_GRADUALLY
from app.models.story import STORY_ARCHIVED, Story
from app.story.content_policy import EXPLICIT_INPUT_MESSAGE, MINORS_INPUT_MESSAGE, check_user_text
from tests.conftest import auth_headers, parse_sse
from tests.test_media import _fichero, _imagen

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


# --- Ficha del autor: leer y editar -----------------------------------------------------


def _patch(client, headers, blueprint_id, cuerpo, esperado=200):
    resp = client.patch(f"/api/custom-stories/{blueprint_id}", json=cuerpo, headers=headers)
    assert resp.status_code == esperado, resp.text
    return resp.json()


def test_detalle_de_una_historia_propia(client):
    headers = auth_headers(client)
    creada = _crear(client, headers, _definida())
    resp = client.get(f"/api/custom-stories/{creada['id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == creada
    assert creada["description"] is None and creada["coverUrl"] is None


@pytest.mark.parametrize(
    "cuerpo, campo, esperado",
    [
        ({"title": "  Otro título  "}, "title", "Otro título"),
        ({"hook": "Un gancho nuevo y suficientemente largo"}, "hook", "Un gancho nuevo y suficientemente largo"),
        ({"description": "  Una novela corta sobre esperar.  "}, "description", "Una novela corta sobre esperar."),
        ({"tone": "seco y luminoso"}, "tone", "seco y luminoso"),
        ({"isPublic": True}, "isPublic", True),
        ({"freeFirstRead": False}, "freeFirstRead", False),
        ({"adult": True}, "adult", True),
    ],
)
def test_patch_campo_a_campo(client, cuerpo, campo, esperado):
    headers = auth_headers(client)
    creada = _crear(client, headers, _definida())
    body = _patch(client, headers, creada["id"], cuerpo)
    assert body[campo] == esperado
    # Solo cambia lo que se manda (publicar sella además la fecha; eso lo prueba el
    # siguiente test).
    intactos = {"definition", "publishedAt"}
    sin_tocar = {k: v for k, v in creada.items() if k not in cuerpo and k not in intactos}
    assert {k: body[k] for k in sin_tocar} == sin_tocar
    # Y se ha guardado de verdad.
    assert client.get(f"/api/custom-stories/{creada['id']}", headers=headers).json()[campo] == esperado


def test_publicar_sella_la_fecha_y_despublicar_no_la_borra(client):
    headers = auth_headers(client)
    creada = _crear(client, headers, _definida())
    publicada = _patch(client, headers, creada["id"], {"isPublic": True})
    assert publicada["publishedAt"] is not None
    despublicada = _patch(client, headers, creada["id"], {"isPublic": False})
    assert despublicada["publishedAt"] == publicada["publishedAt"]


@pytest.mark.parametrize("vacio", [None, "", "   "])
def test_patch_borra_la_descripcion_y_el_tono(client, vacio):
    headers = auth_headers(client)
    creada = _crear(client, headers, _definida())
    _patch(client, headers, creada["id"], {"description": "Algo escrito por la autora."})
    body = _patch(client, headers, creada["id"], {"description": vacio, "tone": vacio})
    assert body["description"] is None and body["tone"] is None
    # El tono vive en el perfil: también sale de la definición.
    assert body["definition"]["tone"] is None


def test_patch_de_personaje_reescribe_el_perfil_y_la_partida_nueva(client, fake_llm):
    headers = auth_headers(client)
    creada = _crear(client, headers, _definida())
    body = _patch(
        client,
        headers,
        creada["id"],
        {
            "name": "Marta Ovejero",
            "age": 41,
            "personality": "seca; divertida cuando confía",
            "speakingStyle": "Habla rápido y se come los finales de las palabras.",
            "setting": "Un puerto pesquero al amanecer, con el hielo aún sin repartir.",
            "backstory": "Heredó el barco de su madre y sigue saliendo a faenar aunque no le haga falta.",
        },
    )
    assert body["definition"]["name"] == "Marta Ovejero"
    assert body["definition"]["age"] == 41
    assert body["definition"]["personality"] == "seca, divertida cuando confía"
    assert body["definition"]["setting"].startswith("Un puerto pesquero")
    # El gancho lo escribió (o lo derivó) la autora: no se re-deriva del escenario nuevo.
    assert body["hook"] == creada["hook"]

    story = client.post("/api/stories", json={"characterId": creada["characterId"]}, headers=headers)
    assert story.status_code == 201, story.text
    story = story.json()
    assert story["characterName"] == "Marta Ovejero"
    # El saludo se narra desde el escenario nuevo.
    assert "puerto pesquero" in story["messages"][0]["content"]

    client.post(f"/api/stories/{story['id']}/chat", json={"message": "Buenos días."}, headers=headers)
    system_prompt = fake_llm.stream_calls[-1][0]
    assert "Marta Ovejero" in system_prompt and "se come los finales" in system_prompt


def test_patch_de_personaje_en_concepto_es_422(client):
    headers = auth_headers(client)
    concepto = _crear(client, headers, {"mode": "concepto", "premise": "Una relojera y un viajero que no sabe que lo es"})
    resp = client.patch(f"/api/custom-stories/{concepto['id']}", json={"name": "Otra"}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "El perfil de una historia en modo concepto lo genera el modelo y no se edita."
    )
    # El tono sí se puede cambiar: lo escribió el autor.
    assert _patch(client, headers, concepto["id"], {"tone": "áspero"})["tone"] == "áspero"


def test_patch_sin_campos_es_422(client):
    headers = auth_headers(client)
    creada = _crear(client, headers, _definida())
    resp = client.patch(f"/api/custom-stories/{creada['id']}", json={}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["msg"] == "Manda al menos un campo que cambiar."


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"description": "x" * 1001},
        {"title": "ab"},
        {"title": None},
        {"hook": "corto"},
        {"age": 17},
        {"name": "Nombre 123"},
        {"setting": "corto"},
        {"campo_inventado": "hola"},
        {"premise": "no se edita"},
    ],
)
def test_patch_con_valores_no_validos_es_422(client, cuerpo):
    headers = auth_headers(client)
    creada = _crear(client, headers, _definida())
    assert client.patch(f"/api/custom-stories/{creada['id']}", json=cuerpo, headers=headers).status_code == 422


def test_la_descripcion_de_1000_caracteres_entra_y_la_de_1001_no(client):
    headers = auth_headers(client)
    creada = _crear(client, headers, _definida())
    assert len(_patch(client, headers, creada["id"], {"description": "x" * 1000})["description"]) == 1000
    resp = client.patch(f"/api/custom-stories/{creada['id']}", json={"description": "x" * 1001}, headers=headers)
    assert "1000 caracteres" in resp.json()["detail"][0]["msg"]


@pytest.mark.parametrize(
    "cuerpo, mensaje",
    [
        ({"description": "Busca sexo con cada cliente que entra por la puerta del local."}, EXPLICIT_INPUT_MESSAGE),
        ({"title": "Romance entre dos adolescentes"}, MINORS_INPUT_MESSAGE),
        ({"backstory": "Conoció a una niña en el parque y desde entonces vuelve cada tarde a buscarla."}, MINORS_INPUT_MESSAGE),
    ],
)
def test_patch_con_texto_prohibido_es_422_y_no_guarda_nada(client, cuerpo, mensaje):
    headers = auth_headers(client)
    creada = _crear(client, headers, _definida())
    resp = client.patch(f"/api/custom-stories/{creada['id']}", json=cuerpo | {"isPublic": True}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"] == mensaje
    # Ni siquiera lo que sí era válido en el mismo PATCH.
    assert client.get(f"/api/custom-stories/{creada['id']}", headers=headers).json()["isPublic"] is False


def test_la_ficha_de_otra_cuenta_es_404(client):
    duena = auth_headers(client)
    otra = auth_headers(client)
    propia = _crear(client, duena, _definida())
    ruta = f"/api/custom-stories/{propia['id']}"
    assert client.get(ruta, headers=otra).status_code == 404
    assert client.get(f"{ruta}/stats", headers=otra).status_code == 404
    assert client.patch(ruta, json={"title": "Secuestrada"}, headers=otra).status_code == 404
    assert client.post(f"{ruta}/cover", files={"file": ("x.png", _portada(), "image/png")}, headers=otra).status_code == 404
    assert client.delete(f"{ruta}/cover", headers=otra).status_code == 404
    assert client.get(ruta, headers=duena).json()["title"] == "Café a medianoche"


def test_la_ficha_de_una_historia_que_no_existe_es_404(client):
    headers = auth_headers(client)
    assert client.get("/api/custom-stories/nohay", headers=headers).status_code == 404
    assert client.get("/api/custom-stories/nohay/stats", headers=headers).status_code == 404


# --- Portada ----------------------------------------------------------------------------


def _portada(**kwargs) -> bytes:
    return _imagen(fmt="PNG", exif=False, **kwargs)


def _subir_portada(client, headers, blueprint_id, data=None, nombre="portada.png", tipo="image/png"):
    return client.post(
        f"/api/custom-stories/{blueprint_id}/cover",
        files={"file": (nombre, _portada() if data is None else data, tipo)},
        headers=headers,
    )


def test_subir_portada_recodifica_a_webp_y_la_devuelve(client):
    headers = auth_headers(client)
    creada = _crear(client, headers, _definida())
    resp = _subir_portada(client, headers, creada["id"], _portada(size=(2400, 1200)))
    assert resp.status_code == 200, resp.text
    url = resp.json()["coverUrl"]
    assert url.startswith("/media/covers/") and url.endswith(".webp")

    servida = client.get(url)
    assert servida.headers["content-type"] == "image/webp"
    with Image.open(BytesIO(servida.content)) as img:
        assert img.format == "WEBP"
        # Sin recortar: la proporción apaisada se conserva y el lado mayor es 1280.
        assert img.size == (1280, 640)
    assert client.get(f"/api/custom-stories/{creada['id']}", headers=headers).json()["coverUrl"] == url


def test_portada_con_tipo_falso_o_demasiado_grande(client, monkeypatch):
    headers = auth_headers(client)
    creada = _crear(client, headers, _definida())
    gif = _imagen("GIF", exif=False)
    resp = _subir_portada(client, headers, creada["id"], gif)
    assert resp.status_code == 415 and resp.json()["detail"][0]["loc"] == ["body", "file"]
    assert _subir_portada(client, headers, creada["id"], b"ni siquiera es una imagen" * 20).status_code == 415

    monkeypatch.setattr(settings, "COVER_MAX_BYTES", 5_000)
    # Por Content-Length, sin llegar a parsear el formulario.
    resp = _subir_portada(client, headers, creada["id"], b"\xff" * 30_000)
    assert resp.status_code == 413 and resp.json()["detail"][0]["type"] == "file_too_large"
    # Dentro del margen del multipart pero por encima del límite: se corta al leer.
    assert _subir_portada(client, headers, creada["id"], _portada() + b"\0" * 6_000).status_code == 413
    assert client.get(f"/api/custom-stories/{creada['id']}", headers=headers).json()["coverUrl"] is None


def test_reemplazar_y_quitar_la_portada_borran_el_fichero(client):
    headers = auth_headers(client)
    creada = _crear(client, headers, _definida())
    primera = _fichero(_subir_portada(client, headers, creada["id"]).json()["coverUrl"])
    segunda = _fichero(_subir_portada(client, headers, creada["id"], _portada(size=(200, 100))).json()["coverUrl"])
    assert primera != segunda
    assert not primera.exists() and segunda.exists()

    assert client.delete(f"/api/custom-stories/{creada['id']}/cover", headers=headers).status_code == 204
    assert not segunda.exists()
    assert client.get(f"/api/custom-stories/{creada['id']}", headers=headers).json()["coverUrl"] is None
    # Quitar una portada que ya no está no falla.
    assert client.delete(f"/api/custom-stories/{creada['id']}/cover", headers=headers).status_code == 204


def test_borrar_la_historia_borra_su_portada(client):
    headers = auth_headers(client)
    creada = _crear(client, headers, _definida())
    fichero = _fichero(_subir_portada(client, headers, creada["id"]).json()["coverUrl"])
    assert client.delete(f"/api/custom-stories/{creada['id']}", headers=headers).status_code == 204
    assert not fichero.exists()


def test_la_portada_sale_en_explorar_en_el_libro_y_en_las_estanterias(client, fake_llm):
    autora = auth_headers(client)
    creada = _crear(client, autora, _definida(isPublic=True))
    url = _subir_portada(client, autora, creada["id"]).json()["coverUrl"]

    lectora = auth_headers(client)
    tarjetas = {c["id"]: c for c in client.get("/api/explore", params={"limit": 50}, headers=lectora).json()["items"]}
    assert tarjetas[creada["id"]]["coverUrl"] == url

    libro = client.get(f"/api/books/{creada['characterId']}", headers=lectora).json()
    assert libro["coverUrl"] == url
    assert client.get("/api/books/lucia", headers=lectora).json()["coverUrl"] is None

    client.post("/api/stories", json={"characterId": creada["characterId"]}, headers=lectora)
    handle = client.get("/api/me/profile", headers=lectora).json()["handle"]
    estanterias = client.get(f"/api/profiles/{handle}", headers=lectora).json()["shelves"]
    [leyendo] = [i for i in estanterias["reading"]["items"] if i["characterId"] == creada["characterId"]]
    assert leyendo["coverUrl"] == url

    autora_handle = client.get("/api/me/profile", headers=autora).json()["handle"]
    publicadas = client.get(f"/api/profiles/{autora_handle}", headers=autora).json()["shelves"]["published"]["items"]
    assert [c["coverUrl"] for c in publicadas if c["id"] == creada["id"]] == [url]


# --- Estadísticas -----------------------------------------------------------------------


def test_estadisticas_de_una_historia_propia(client, fake_llm):
    autora = auth_headers(client)
    creada = _crear(client, autora, _definida(isPublic=True))
    ruta = f"/api/custom-stories/{creada['id']}/stats"

    vacias = client.get(ruta, headers=autora).json()
    assert vacias == {
        "readers": 0, "activeStories": 0, "ratingAverage": None, "reviewCount": 0, "recentReviews": []
    }

    primera, segunda = auth_headers(client), auth_headers(client)
    for lectora in (primera, segunda):
        resp = client.post("/api/stories", json={"characterId": creada["characterId"]}, headers=lectora)
        assert resp.status_code == 201, resp.text
    # La segunda deja de tener partida activa: cuenta como lectora, no como partida en curso.
    with SessionLocal() as db:
        story = db.scalars(
            select(Story).where(Story.character_id == creada["characterId"]).order_by(Story.created_at.desc())
        ).first()
        story.status = STORY_ARCHIVED
        db.commit()

    for lectora, nota, texto in ((primera, 5, "Me la he leído dos veces."), (segunda, 4, None)):
        resp = client.put(
            f"/api/books/{creada['characterId']}/reviews/me",
            json={"rating": nota, "text": texto},
            headers=lectora,
        )
        assert resp.status_code in (200, 201), resp.text

    stats = client.get(ruta, headers=autora).json()
    assert stats["readers"] == 2
    assert stats["activeStories"] == 1
    assert stats["ratingAverage"] == 4.5
    assert stats["reviewCount"] == 2
    # Las más recientes primero, con el autor de cada una.
    assert [r["rating"] for r in stats["recentReviews"]] == [4, 5]
    assert stats["recentReviews"][-1]["text"] == "Me la he leído dos veces."
    assert stats["recentReviews"][0]["author"]["handle"]
    assert all(r["isMine"] is False for r in stats["recentReviews"])
