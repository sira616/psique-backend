"""Resumen rodante: plegado por lotes tras `done`, validación, fallos y contexto con hueco."""
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, text

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.migrations import alembic_config
from app.llm import router as llm_router
from app.llm.prompts.summary import SUMMARY_SYSTEM_PROMPT
from app.models.story import Message, Story
from app.services import chat_stream_service, summary_service
from app.story import context
from tests.conftest import auth_headers, parse_sse

RESUMEN_PREVIO = "Se conocieron en el taller de encuadernación."
RESUMEN_NUEVO = "Lucía le enseñó la carta escondida. La persona prometió volver el domingo."


class ResumenLLM:
    """Envuelve el doble del router: responde a la llamada de resumen y deja pasar el resto."""

    def __init__(self, fake):
        self.fake = fake
        self.respuesta: str = RESUMEN_NUEVO
        self.error: Exception | None = None
        self.llamadas: list[str] = []
        self.al_llamar = None

    def generate(self, system_prompt, messages, *, max_tokens=None):
        if system_prompt != SUMMARY_SYSTEM_PROMPT:
            return self.fake.generate(system_prompt, messages, max_tokens=max_tokens)
        self.llamadas.append(messages[0]["content"])
        if self.al_llamar:
            self.al_llamar()
        if self.error:
            raise self.error
        return self.respuesta


@pytest.fixture
def resumen_llm(fake_llm, monkeypatch):
    # Ventana y lote pequeños para no tener que sembrar decenas de mensajes.
    monkeypatch.setattr(settings, "CONTEXT_MESSAGES", 4)
    monkeypatch.setattr(settings, "SUMMARY_BATCH_MESSAGES", 4)
    monkeypatch.setattr(llm_router, "demo_mode", lambda: False)
    doble = ResumenLLM(fake_llm)
    monkeypatch.setattr(llm_router, "generate", doble.generate)
    return doble


def _historia(client, headers) -> str:
    resp = client.post("/api/stories", json={"characterId": "lucia"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _sembrar(story_id: str, n: int, *, resumen: str = "", cubre_saludo: bool = False) -> list[int]:
    """Añade `n` mensajes antiguos alternando roles y devuelve sus ids."""
    with SessionLocal() as db:
        story = db.get(Story, story_id)
        saludo = context.load_messages(db, story_id)[0]
        if cubre_saludo:
            story.summary, story.summary_upto_message_id = resumen, saludo.id
        nuevos = [
            Message(story_id=story_id, role="user" if i % 2 else "assistant", content=f"Mensaje antiguo {i}")
            for i in range(1, n + 1)
        ]
        db.add_all(nuevos)
        db.commit()
        return [m.id for m in nuevos]


def _resumen(story_id: str) -> tuple[str, int | None]:
    with SessionLocal() as db:
        story = db.get(Story, story_id)
        return story.summary, story.summary_upto_message_id


def _turno(client, headers, story_id, mensaje="Sigue") -> list[tuple[str, dict]]:
    resp = client.post(f"/api/stories/{story_id}/chat", json={"message": mensaje}, headers=headers)
    assert resp.status_code == 200, resp.text
    return parse_sse(resp.text)


# --- Validación -------------------------------------------------------------------------


def test_validacion_recorta_por_frase_completa():
    frase = "Lucía y la persona pasearon por el puerto hablando de libros antiguos. "
    largo = frase * (context.SUMMARY_MAX_CHARS // len(frase) + 5)
    limpio = summary_service.validate_summary(largo)
    assert limpio is not None
    assert len(limpio) <= context.SUMMARY_MAX_CHARS
    assert limpio.endswith("antiguos.")


@pytest.mark.parametrize(
    "crudo",
    [
        "",
        "   ",
        "a" * (context.SUMMARY_MAX_CHARS + 10),  # sin ningún final de frase donde cortar
        "Pasaron la noche juntos y hubo un orgasmo.",
        "La persona le dio su correo, ana.perez@example.com, para escribirse.",
        "Le dejó su teléfono 612 345 678 apuntado en la carta.",
    ],
)
def test_validacion_descarta_vacio_sin_frases_explicito_y_datos_personales(crudo):
    assert summary_service.validate_summary(crudo) is None


def test_validacion_quita_la_etiqueta_que_antepone_el_modelo():
    assert summary_service.validate_summary("**Resumen actualizado:** Se vieron en el taller.") == (
        "Se vieron en el taller."
    )


# --- Plegado ----------------------------------------------------------------------------


def test_se_pliega_al_llegar_al_lote_y_el_contexto_usa_resumen_y_hueco(client, resumen_llm, fake_llm):
    headers = auth_headers(client)
    story_id = _historia(client, headers)
    ids = _sembrar(story_id, 6, resumen=RESUMEN_PREVIO, cubre_saludo=True)

    # Antes del turno solo hay 2 sin resumir fuera de la ventana: no llega al lote.
    with SessionLocal() as db:
        story = db.get(Story, story_id)
        pendientes = context.unsummarized(context.load_messages(db, story_id), 4, story.summary_upto_message_id)
    assert len(pendientes) == 2

    # Tras el turno hay 9 mensajes: fuera de la ventana, el saludo (ya resumido) y 4 más.
    events = _turno(client, headers, story_id)
    assert [n for n, _ in events][-2:] == ["state", "done"]
    assert len(resumen_llm.llamadas) == 1
    entrada = resumen_llm.llamadas[0]
    assert RESUMEN_PREVIO in entrada
    assert "PERSONAJE: Lucía" in entrada
    for i in range(1, 5):
        assert f"Mensaje antiguo {i}" in entrada
    assert "Mensaje antiguo 5" not in entrada
    assert _resumen(story_id) == (RESUMEN_NUEVO, ids[3])

    # Siguiente turno: resumen guardado + recorte del hueco (el 5), lo resumido ya no va
    # literal y lo que está en la ventana no se duplica.
    _turno(client, headers, story_id, "¿Y la carta?")
    system_prompt = fake_llm.stream_calls[-1][0]
    assert f"LO OCURRIDO ANTES (resumen):\n{RESUMEN_NUEVO}" in system_prompt
    assert "Mensaje antiguo 5" in system_prompt
    assert "Mensaje antiguo 1" not in system_prompt
    assert "Mensaje antiguo 6" not in system_prompt
    # Hueco de 2 < lote: no hubo otra llamada.
    assert len(resumen_llm.llamadas) == 1


def test_los_datos_sensibles_no_llegan_al_prompt_del_resumen(client, resumen_llm):
    headers = auth_headers(client)
    story_id = _historia(client, headers)
    _sembrar(story_id, 5)
    _turno(client, headers, story_id, "Escríbeme a ana.perez@example.com")
    _turno(client, headers, story_id)
    _turno(client, headers, story_id)
    # El segundo plegado es el que incluye el mensaje con el correo.
    assert len(resumen_llm.llamadas) == 2
    assert "[dato privado]" in resumen_llm.llamadas[1]
    assert all("ana.perez@example.com" not in entrada for entrada in resumen_llm.llamadas)


@pytest.mark.parametrize(
    "fallo",
    [
        {"error": llm_router.LLMUnavailableError("apagado")},
        {"error": llm_router.LLMRefusalError("no")},
        {"error": RuntimeError("inesperado")},
        {"respuesta": ""},
        {"respuesta": "Hubo un orgasmo en el taller."},
        {"respuesta": "Le dio su correo ana@example.com."},
    ],
)
def test_un_fallo_conserva_resumen_y_marcador_y_se_reintenta_despues(client, resumen_llm, fallo):
    headers = auth_headers(client)
    story_id = _historia(client, headers)
    ids = _sembrar(story_id, 6, resumen=RESUMEN_PREVIO, cubre_saludo=True)
    antes = _resumen(story_id)
    resumen_llm.error = fallo.get("error")
    resumen_llm.respuesta = fallo.get("respuesta", RESUMEN_NUEVO)

    events = _turno(client, headers, story_id)
    assert [n for n, _ in events][-2:] == ["state", "done"]
    assert "error" not in [n for n, _ in events]
    assert len(resumen_llm.llamadas) == 1
    assert _resumen(story_id) == antes

    # Sin reintentos inmediatos: el siguiente intento es en otro turno, y ahí avanza.
    resumen_llm.error, resumen_llm.respuesta = None, RESUMEN_NUEVO
    _turno(client, headers, story_id)
    assert len(resumen_llm.llamadas) == 2
    assert "Mensaje antiguo 5" in resumen_llm.llamadas[1]
    summary, marcador = _resumen(story_id)
    assert summary == RESUMEN_NUEVO and marcador > ids[3]


def test_el_resumen_se_pide_despues_de_emitir_done(client, resumen_llm):
    headers = auth_headers(client)
    story_id = _historia(client, headers)
    _sembrar(story_id, 6, resumen=RESUMEN_PREVIO, cubre_saludo=True)

    emitidos: list[str] = []
    vistos_al_llamar: list[list[str]] = []
    resumen_llm.al_llamar = lambda: vistos_al_llamar.append(list(emitidos))
    for event in chat_stream_service.stream_turn(story_id, "Hola"):
        emitidos.append(event["event"])

    assert len(vistos_al_llamar) == 1
    assert vistos_al_llamar[0][-2:] == ["state", "done"]
    assert _resumen(story_id)[0] == RESUMEN_NUEVO


def test_modo_demo_da_un_resumen_determinista_sin_llamar_al_llm(client, fake_llm, monkeypatch):
    monkeypatch.setattr(settings, "CONTEXT_MESSAGES", 4)
    monkeypatch.setattr(settings, "SUMMARY_BATCH_MESSAGES", 4)
    headers = auth_headers(client)
    story_id = _historia(client, headers)
    ids = _sembrar(story_id, 6)

    _turno(client, headers, story_id)
    assert all(call[0] != SUMMARY_SYSTEM_PROMPT for call in fake_llm.generate_calls)
    summary, marcador = _resumen(story_id)
    assert marcador == ids[3]
    assert summary.splitlines()[-1] == "Personaje: Mensaje antiguo 4"
    assert summary.startswith("Personaje: ")


# --- Partidas ---------------------------------------------------------------------------


def test_releer_empieza_sin_resumen_y_la_archivada_conserva_el_suyo(client, fake_llm):
    headers = auth_headers(client)
    vieja = _historia(client, headers)
    _sembrar(vieja, 0, resumen=RESUMEN_PREVIO, cubre_saludo=True)
    antes = _resumen(vieja)

    resp = client.post("/api/books/lucia/reread", headers=headers)
    assert resp.status_code == 201, resp.text
    nueva = resp.json()["id"]
    assert _resumen(nueva) == ("", None)
    assert _resumen(vieja) == antes
    assert "summary" not in resp.json()

    _turno(client, headers, nueva)
    assert RESUMEN_PREVIO not in fake_llm.stream_calls[-1][0]


def test_la_migracion_deja_las_partidas_existentes_sin_marcador(tmp_path: Path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'resumen.db').as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = alembic_config()
    command.upgrade(config, "5e1c9a7b3d20")
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO users (id, username, password_hash, display_name, handle) "
                "VALUES ('u1', 'lectora', 'x', 'Lectora', 'lectora')"
            ))
            conn.execute(text(
                "INSERT INTO stories (id, user_id, character_id, phase, affinity, turn_count, summary) "
                "VALUES ('s1', 'u1', 'lucia', 'conocerse', 20, 0, 'algo')"
            ))
        command.upgrade(config, "head")
        with engine.connect() as conn:
            fila = conn.execute(text("SELECT summary, summary_upto_message_id FROM stories")).one()
        assert tuple(fila) == ("algo", None)
        command.downgrade(config, "5e1c9a7b3d20")
    finally:
        engine.dispose()
