"""Escena en curso y sugerencias: las redacta el extractor, las valida y pesa el código."""
import json

import pytest
from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.story import STORY_ARCHIVED, Story, StoryEvent
from app.story import memory, scene
from app.story import state_machine as sm
from tests.conftest import auth_headers, parse_sse

ESCENA = "En el taller: la carta escondida"
SUGERENCIAS = [
    {"intent": "preguntar", "label": "Preguntar por la carta", "message": "¿De quién es esa carta que escondes entre las páginas?"},
    {"intent": "reconciliar", "label": "Quitarle hierro", "message": "*Levanto las manos* No quería incomodarte. ¿Lo hablamos con calma?"},
    {"intent": "humor", "label": "Bromear con el misterio", "message": "Si es un mapa del tesoro, pido la mitad."},
]
RESERVA_CONOCERSE = [c.message for c in sm.choices_for(sm.Phase.CONOCERSE)]


def _historia(client, headers):
    resp = client.post("/api/stories", json={"characterId": "lucia"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _turno(client, headers, story_id, **payload):
    resp = client.post(f"/api/stories/{story_id}/chat", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return dict(parse_sse(resp.text))["state"]


# --- Validación pura ----------------------------------------------------------------------


def test_extraccion_valida_trae_escena_y_tres_sugerencias():
    raw = json.dumps({"hechos": {}, "senales": [], "scene": ESCENA, "suggestions": SUGERENCIAS})
    result = memory.parse_extraction(raw)
    assert result.scene == ESCENA
    assert [c.intent for c in result.suggestions] == ["preguntar", "reconciliar", "humor"]


@pytest.mark.parametrize(
    "sugerencias",
    [
        SUGERENCIAS[:2],
        SUGERENCIAS + [SUGERENCIAS[0] | {"intent": "cumplido"}],
        [SUGERENCIAS[0], SUGERENCIAS[1], SUGERENCIAS[2] | {"intent": "declararse"}],
        [SUGERENCIAS[0], SUGERENCIAS[1], SUGERENCIAS[2] | {"intent": "preguntar"}],
        [SUGERENCIAS[0], SUGERENCIAS[1], SUGERENCIAS[2] | {"label": SUGERENCIAS[0]["label"]}],
        [SUGERENCIAS[0], SUGERENCIAS[1], SUGERENCIAS[2] | {"label": "x" * 41}],
        [SUGERENCIAS[0], SUGERENCIAS[1], SUGERENCIAS[2] | {"message": "x" * 221}],
        [SUGERENCIAS[0], SUGERENCIAS[1], SUGERENCIAS[2] | {"message": "Me quito la ropa, sin ropa ya."}],
        [SUGERENCIAS[0], SUGERENCIAS[1], SUGERENCIAS[2] | {"message": "Escríbeme a marta@example.com"}],
        [SUGERENCIAS[0], SUGERENCIAS[1], {"intent": "humor", "label": "Sin mensaje"}],
        "tres sugerencias",
        None,
    ],
)
def test_sugerencias_invalidas_se_descartan_enteras(sugerencias):
    assert scene.validate_suggestions(sugerencias) is None


def test_escena_invalida_se_descarta():
    assert scene.validate_scene("x" * 61) is None
    assert scene.validate_scene("") is None
    assert scene.validate_scene(42) is None
    assert scene.validate_scene("Una escena de sexo") is None
    assert scene.validate_scene(f"  {ESCENA}  ") == ESCENA


def test_el_extractor_recibe_la_escena_anterior_la_fase_y_la_respuesta(fake_llm):
    memory.extract_turn(
        "Hola", "¿Qué es eso?", "*Esconde la carta.*", previous_scene=ESCENA, phase=sm.Phase.CONFIANZA, character_name="Lucía"
    )
    system_prompt, messages = fake_llm.generate_calls[0]
    assert "reconciliar" in system_prompt and "EXACTAMENTE igual" in system_prompt
    contenido = messages[0]["content"]
    assert f"ESCENA ANTERIOR: {ESCENA}" in contenido
    assert "FASE: Confianza" in contenido
    assert "PERSONAJE (respuesta): *Esconde la carta.*" in contenido
    # El contexto que pone el código queda fuera del bloque que se analiza.
    assert contenido.index("ESCENA ANTERIOR") < contenido.index("<turno>")


# --- De punta a punta ---------------------------------------------------------------------


def test_primer_turno_usa_la_reserva_de_la_fase_con_escena_inicial(client):
    story = _historia(client, auth_headers(client))
    state = story["state"]
    assert state["scene"] == "Primer encuentro con Lucía Ferrer"
    assert [c["id"] for c in state["quickChoices"]] == ["t0s1", "t0s2", "t0s3"]
    assert [c["message"] for c in state["quickChoices"]] == RESERVA_CONOCERSE


def test_extraccion_valida_se_guarda_y_la_eleccion_usa_el_texto_guardado(client, fake_llm):
    headers = auth_headers(client)
    story = _historia(client, headers)
    fake_llm.extraction = {"hechos": {}, "senales": [], "scene": ESCENA, "suggestions": SUGERENCIAS}

    state = _turno(client, headers, story["id"], message="¿Qué escondes ahí?")
    assert state["scene"] == ESCENA
    assert state["quickChoices"] == [
        {"id": f"t1s{n}", "label": s["label"], "message": s["message"]} for n, s in enumerate(SUGERENCIAS, start=1)
    ]
    assert "intent" not in state["quickChoices"][0]
    detalle = client.get(f"/api/stories/{story['id']}", headers=headers).json()
    assert detalle["state"]["scene"] == ESCENA
    assert detalle["state"]["quickChoices"] == state["quickChoices"]

    # Una sugerencia del turno anterior ya no vale.
    viejo = client.post(f"/api/stories/{story['id']}/chat", json={"choiceId": "t0s1"}, headers=headers)
    assert viejo.status_code == 422

    afinidad = state["affinity"]
    fake_llm.extraction = {"hechos": {}, "senales": []}
    nuevo = _turno(client, headers, story["id"], choiceId="t1s2")
    # El peso lo pone la intención en código, no el modelo; el texto es el guardado.
    assert nuevo["affinity"] == afinidad + sm.INTENT_WEIGHTS["reconciliar"] + 1  # +1 por dos turnos
    assert fake_llm.stream_calls[-1][1][-1]["content"] == SUGERENCIAS[1]["message"]
    detalle = client.get(f"/api/stories/{story['id']}", headers=headers).json()
    assert detalle["messages"][3]["content"] == SUGERENCIAS[1]["message"]
    with SessionLocal() as db:
        decisiones = list(
            db.scalars(select(StoryEvent.name).where(StoryEvent.story_id == story["id"], StoryEvent.kind == "decision"))
        )
    assert decisiones == ["reconciliar"]

    # Sin sugerencias válidas: reserva de la fase, pero la escena se conserva.
    assert nuevo["scene"] == ESCENA
    assert [c["id"] for c in nuevo["quickChoices"]] == ["t2s1", "t2s2", "t2s3"]
    assert [c["message"] for c in nuevo["quickChoices"]] == RESERVA_CONOCERSE


@pytest.mark.parametrize(
    "extraccion",
    [
        {"hechos": {}, "senales": [], "scene": ESCENA, "suggestions": SUGERENCIAS[:2]},
        {"hechos": {}, "senales": [], "scene": ESCENA, "suggestions": [SUGERENCIAS[0]] * 3},
        {"hechos": {}, "senales": [], "scene": ESCENA, "suggestions": SUGERENCIAS[:2] + [SUGERENCIAS[2] | {"intent": "besar"}]},
        {"hechos": {}, "senales": [], "scene": ESCENA, "suggestions": SUGERENCIAS[:2] + [SUGERENCIAS[2] | {"message": "Me desnudo"}]},
    ],
)
def test_sugerencias_invalidas_caen_a_la_reserva_de_la_fase(client, fake_llm, extraccion):
    headers = auth_headers(client)
    story = _historia(client, headers)
    fake_llm.extraction = extraccion
    state = _turno(client, headers, story["id"], message="Hola")
    assert state["scene"] == ESCENA
    assert [c["message"] for c in state["quickChoices"]] == RESERVA_CONOCERSE


def test_json_roto_mantiene_la_escena_y_usa_la_reserva(client, fake_llm, monkeypatch):
    headers = auth_headers(client)
    story = _historia(client, headers)
    fake_llm.extraction = {"hechos": {}, "senales": [], "scene": ESCENA, "suggestions": SUGERENCIAS}
    _turno(client, headers, story["id"], message="Hola")

    monkeypatch.setattr("app.llm.router.generate", lambda *a, **k: '{"scene": "Otra", "suggestions": [')
    state = _turno(client, headers, story["id"], message="Sigue")
    assert state["scene"] == ESCENA
    assert [c["id"] for c in state["quickChoices"]] == ["t2s1", "t2s2", "t2s3"]
    assert [c["message"] for c in state["quickChoices"]] == RESERVA_CONOCERSE


def test_modo_demo_da_escena_y_sugerencias_deterministas(client):
    headers = auth_headers(client)
    story = _historia(client, headers)
    state = _turno(client, headers, story["id"], message="Hola")
    assert state["scene"] == "Primer encuentro con Lucía Ferrer"
    assert [c["message"] for c in state["quickChoices"]] == RESERVA_CONOCERSE


def test_al_desbloquear_la_reserva_pasa_a_la_fase_nueva(client, fake_llm):
    headers = auth_headers(client)
    story = _historia(client, headers)
    with SessionLocal() as db:
        db.get(Story, story["id"]).pending_phase = "confianza"
        db.commit()
    state = client.post(f"/api/stories/{story['id']}/unlock-chapter", headers=headers).json()
    assert state["phase"] == "confianza"
    assert state["scene"] == "Ganando confianza con Lucía Ferrer"
    assert [c["message"] for c in state["quickChoices"]] == [c.message for c in sm.choices_for(sm.Phase.CONFIANZA)]


def test_partida_archivada_no_ofrece_sugerencias(client):
    headers = auth_headers(client)
    story = _historia(client, headers)
    with SessionLocal() as db:
        db.get(Story, story["id"]).status = STORY_ARCHIVED
        db.commit()
    state = client.get(f"/api/stories/{story['id']}", headers=headers).json()["state"]
    assert state["quickChoices"] == []
    assert state["scene"]


def test_partida_anterior_a_las_escenas_recibe_la_reserva(client, fake_llm):
    headers = auth_headers(client)
    story = _historia(client, headers)
    with SessionLocal() as db:
        fila = db.get(Story, story["id"])
        fila.scene_title = fila.suggestions = fila.suggestions_turn = None
        db.commit()
    state = client.get(f"/api/stories/{story['id']}", headers=headers).json()["state"]
    assert state["scene"] == "Primer encuentro con Lucía Ferrer"
    assert [c["id"] for c in state["quickChoices"]] == ["t0s1", "t0s2", "t0s3"]
    assert _turno(client, headers, story["id"], choiceId="t0s1")["affinity"] == sm.BASE_AFFINITY + 2
