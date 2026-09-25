"""Libros: capítulo bloqueado, una partida activa por libro, releer, historial, reseñas y
recomendados."""
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.migrations import alembic_config
from app.models.story import Message, Story, StoryEvent
from app.services import economy_service
from app.story.content_policy import EXPLICIT_INPUT_MESSAGE
from tests.conftest import parse_sse, register
from tests.test_custom_stories import SECRETO, _definida
from tests.test_explore import PREMISA, llm_concepto  # noqa: F401 — fixture


def _cuenta(client) -> tuple[dict, dict]:
    sesion = register(client)
    return sesion["user"], {"Authorization": f"Bearer {sesion['access_token']}"}


def _empezar(client, headers, book_id="lucia", esperado=201) -> dict:
    resp = client.post("/api/stories", json={"characterId": book_id}, headers=headers)
    assert resp.status_code == esperado, resp.text
    return resp.json()


def _crear_libro(client, headers, **cambios) -> dict:
    resp = client.post("/api/custom-stories", json=_definida(**cambios), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _saldo(client, headers) -> int:
    return client.get("/api/me/wallet", headers=headers).json()["balance"]


def _vaciar_saldo(user_id: str) -> None:
    with SessionLocal() as db:
        saldo = economy_service.balance(db, user_id)
        if saldo:
            economy_service.spend(db, user_id, saldo, "ajuste_dev")
        db.commit()


def _bloquear(story_id: str) -> None:
    with SessionLocal() as db:
        db.get(Story, story_id).pending_phase = "confianza"
        db.commit()


def _foto(story_id: str) -> tuple:
    with SessionLocal() as db:
        story = db.get(Story, story_id)
        mensajes = db.scalar(select(func.count()).where(Message.story_id == story_id))
        eventos = db.scalar(select(func.count()).where(StoryEvent.story_id == story_id))
        return story.affinity, story.turn_count, story.phase, story.pending_phase, mensajes, eventos


def _libro(client, headers, book_id="lucia", esperado=200) -> dict:
    resp = client.get(f"/api/books/{book_id}", headers=headers)
    assert resp.status_code == esperado, resp.text
    return resp.json()


# --- Fallo 1: capítulo bloqueado --------------------------------------------------------


def test_chat_con_capitulo_bloqueado_es_409_sin_llm_ni_cambios(client, fake_llm):
    _, headers = _cuenta(client)
    story = _empezar(client, headers)
    fake_llm.extraction = {"hechos": {}, "senales": ["cumplido"]}
    client.post(f"/api/stories/{story['id']}/chat", json={"message": "Hola"}, headers=headers)
    _bloquear(story["id"])
    fake_llm.stream_calls.clear()
    fake_llm.generate_calls.clear()
    antes = _foto(story["id"])

    eleccion = client.get(f"/api/stories/{story['id']}", headers=headers).json()["state"]["quickChoices"][0]["id"]
    for cuerpo in ({"message": "Sigo hablando"}, {"choiceId": eleccion}):
        resp = client.post(f"/api/stories/{story['id']}/chat", json=cuerpo, headers=headers)
        assert resp.status_code == 409
        assert resp.headers["content-type"].startswith("application/json")
        body = resp.json()
        assert body["code"] == "chapter_locked"
        assert body["detail"] == "Este capítulo está bloqueado. Desbloquéalo para seguir la historia."
        assert (body["state"]["chapter_locked"], body["state"]["next_phase"]) == (True, "confianza")

    assert not fake_llm.stream_calls and not fake_llm.generate_calls
    assert _foto(story["id"]) == antes


def test_un_turno_en_curso_no_suma_si_otro_bloquea_el_capitulo(client, fake_llm, monkeypatch):
    _, headers = _cuenta(client)
    story = _empezar(client, headers)
    afinidad = _foto(story["id"])[0]
    extraccion = fake_llm.generate

    def bloquear_durante_la_extraccion(system_prompt, messages, *, max_tokens=None):
        # Otro turno simultáneo gana el capítulo mientras este aún no ha aplicado su estado.
        _bloquear(story["id"])
        return extraccion(system_prompt, messages, max_tokens=max_tokens)

    monkeypatch.setattr("app.llm.router.generate", bloquear_durante_la_extraccion)
    fake_llm.extraction = {"hechos": {}, "senales": ["cumplido", "vulnerabilidad"]}

    fake_llm.extraction |= {"scene": "Otra escena", "suggestions": []}
    eleccion = story["state"]["quickChoices"][0]["id"]
    resp = client.post(f"/api/stories/{story['id']}/chat", json={"choiceId": eleccion}, headers=headers)
    assert resp.status_code == 200
    state = dict(parse_sse(resp.text))["state"]
    assert state["chapter_locked"] is True
    assert state["signals"] == []
    assert state["affinity"] == afinidad

    with SessionLocal() as db:
        tipos = set(db.scalars(select(StoryEvent.kind).where(StoryEvent.story_id == story["id"])))
    assert tipos == {"turno"}
    # Tampoco escribe escena ni sugerencias: siguen las del turno anterior.
    assert state["scene"] != "Otra escena"
    assert state["quickChoices"][0]["id"] == eleccion


def test_partida_archivada_no_admite_chat_ni_desbloqueo(client, fake_llm):
    _, headers = _cuenta(client)
    vieja = _empezar(client, headers)
    _bloquear(vieja["id"])
    assert client.post("/api/books/lucia/reread", headers=headers).status_code == 201

    detalle = client.get(f"/api/stories/{vieja['id']}", headers=headers).json()
    assert detalle["status"] == "archivada" and detalle["archivedAt"] is not None
    for ruta, cuerpo in (("chat", {"message": "Hola"}), ("unlock-chapter", None)):
        resp = client.post(f"/api/stories/{vieja['id']}/{ruta}", json=cuerpo, headers=headers)
        assert resp.status_code == 409
        assert resp.json()["code"] == "story_archived"
    assert not fake_llm.stream_calls


# --- Una partida activa por libro -------------------------------------------------------


def test_post_repetido_devuelve_la_misma_partida_sin_cobrar(client):
    _, headers = _cuenta(client)
    primera = _empezar(client, headers)
    assert (primera["status"], primera["archivedAt"]) == ("activa", None)
    segunda = _empezar(client, headers, esperado=200)
    assert segunda["id"] == primera["id"]
    assert [s["id"] for s in client.get("/api/stories", headers=headers).json()] == [primera["id"]]
    assert _saldo(client, headers) == settings.WELCOME_OBOLOS


def test_indice_unico_impide_dos_activas_del_mismo_libro(client):
    user, headers = _cuenta(client)
    _empezar(client, headers)
    with SessionLocal() as db:
        db.add(Story(id="duplicada-" + user["id"][:20], user_id=user["id"], character_id="lucia",
                     phase="conocerse", affinity=20, turn_count=0, summary=""))
        with pytest.raises(IntegrityError):
            db.commit()


def test_primera_lectura_gratis_o_de_pago_segun_el_libro(client):
    _, autora = _cuenta(client)
    gratis = _crear_libro(client, autora, isPublic=True)
    de_pago = _crear_libro(client, autora, isPublic=True, freeFirstRead=False)
    assert (gratis["freeFirstRead"], de_pago["freeFirstRead"]) == (True, False)

    _, lectora = _cuenta(client)
    assert _libro(client, lectora, de_pago["characterId"])["viewer"]["primaryAction"] == {
        "kind": "leer", "cost": settings.READ_COST
    }
    _empezar(client, lectora, gratis["characterId"])
    assert _saldo(client, lectora) == settings.WELCOME_OBOLOS

    story = _empezar(client, lectora, de_pago["characterId"])
    wallet = client.get("/api/me/wallet", headers=lectora).json()
    assert wallet["balance"] == settings.WELCOME_OBOLOS - settings.READ_COST
    assert (wallet["movements"][0]["amount"], wallet["movements"][0]["reason"], wallet["movements"][0]["reference"]) == (
        -settings.READ_COST, "lectura", story["id"]
    )


def test_sin_saldo_para_un_libro_de_pago_es_402_y_no_crea_nada(client):
    _, autora = _cuenta(client)
    de_pago = _crear_libro(client, autora, isPublic=True, freeFirstRead=False)
    lectora, headers = _cuenta(client)
    _vaciar_saldo(lectora["id"])
    resp = client.post("/api/stories", json={"characterId": de_pago["characterId"]}, headers=headers)
    assert resp.status_code == 402
    assert resp.json() == {"detail": "Te faltan óbolos para empezar este libro. Puedes ganar más en Rasca y gana."}
    assert client.get("/api/stories", headers=headers).json() == []


def test_patch_free_first_read(client):
    _, autora = _cuenta(client)
    libro = _crear_libro(client, autora, isPublic=True)
    url = f"/api/custom-stories/{libro['id']}"
    resp = client.patch(url, json={"freeFirstRead": False}, headers=autora)
    assert resp.status_code == 200
    assert (resp.json()["freeFirstRead"], resp.json()["isPublic"]) == (False, True)
    assert client.patch(url, json={}, headers=autora).status_code == 422
    assert client.patch(url, json={"isPublic": None}, headers=autora).status_code == 422

    _, lectora = _cuenta(client)
    _empezar(client, lectora, libro["characterId"])
    assert _saldo(client, lectora) == settings.WELCOME_OBOLOS - settings.READ_COST


# --- Releer -----------------------------------------------------------------------------


def test_releer_archiva_la_activa_y_cobra(client):
    _, headers = _cuenta(client)
    vieja = _empezar(client, headers)
    resp = client.post("/api/books/lucia/reread", headers=headers)
    assert resp.status_code == 201, resp.text
    nueva = resp.json()
    assert nueva["id"] != vieja["id"] and nueva["status"] == "activa"
    assert [m["role"] for m in nueva["messages"]] == ["assistant"]

    wallet = client.get("/api/me/wallet", headers=headers).json()
    assert wallet["balance"] == settings.WELCOME_OBOLOS - settings.READ_COST
    assert (wallet["movements"][0]["reason"], wallet["movements"][0]["reference"]) == ("lectura", nueva["id"])

    assert [s["id"] for s in client.get("/api/stories", headers=headers).json()] == [nueva["id"]]
    historial = client.get("/api/books/lucia/history", headers=headers).json()
    assert [h["storyId"] for h in historial] == [vieja["id"]]
    assert set(historial[0]) == {"storyId", "status", "startedAt", "archivedAt", "closedAt", "phase", "phaseLabel", "phaseIndex", "phaseCount", "affinity"}

    # Después de releer, empezar de nuevo devuelve la activa y no cobra.
    assert _empezar(client, headers, esperado=200)["id"] == nueva["id"]


def test_releer_sin_saldo_es_402_y_no_archiva(client):
    user, headers = _cuenta(client)
    activa = _empezar(client, headers)
    _vaciar_saldo(user["id"])
    resp = client.post("/api/books/lucia/reread", headers=headers)
    assert resp.status_code == 402
    assert client.get(f"/api/stories/{activa['id']}", headers=headers).json()["status"] == "activa"
    assert client.get("/api/books/lucia/history", headers=headers).json() == []
    assert _libro(client, headers)["viewer"]["activeStoryId"] == activa["id"]


def test_releer_sin_haber_empezado_es_409_y_libro_invisible_404(client):
    _, headers = _cuenta(client)
    resp = client.post("/api/books/lucia/reread", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["code"] == "not_started"
    assert client.post("/api/books/nadie/reread", headers=headers).status_code == 404


def test_historial_privado(client):
    _, mia = _cuenta(client)
    _empezar(client, mia)
    client.post("/api/books/lucia/reread", headers=mia)
    assert len(client.get("/api/books/lucia/history", headers=mia).json()) == 1

    _, otra = _cuenta(client)
    _empezar(client, otra)
    assert client.get("/api/books/lucia/history", headers=otra).json() == []


def test_leyendo_solo_muestra_partidas_activas(client):
    user, headers = _cuenta(client)
    _empezar(client, headers)
    client.post("/api/books/lucia/reread", headers=headers)
    perfil = client.get(f"/api/profiles/{user['handle']}", headers=headers).json()
    assert [i["characterId"] for i in perfil["shelves"]["reading"]["items"]] == ["lucia"]


# --- Migración --------------------------------------------------------------------------


def test_la_migracion_archiva_duplicadas_y_deja_la_mas_reciente(tmp_path: Path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'duplicadas.db').as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    config = alembic_config()
    command.upgrade(config, "7d3307cbd054")
    engine = create_engine(url)
    partidas = [
        # id, libro, created_at, updated_at
        ("a-vieja", "lucia", "2026-09-01 10:00:00", "2026-09-02 10:00:00"),
        ("a-nueva", "lucia", "2026-09-01 11:00:00", "2026-09-05 10:00:00"),
        ("a-media", "lucia", "2026-09-03 10:00:00", "2026-09-04 10:00:00"),
        # Empate en updated_at: decide created_at.
        ("b-antes", "mateo", "2026-09-01 10:00:00", "2026-09-06 10:00:00"),
        ("b-despues", "mateo", "2026-09-02 10:00:00", "2026-09-06 10:00:00"),
        ("c-sola", "custom:abc", "2026-09-01 10:00:00", "2026-09-01 10:00:00"),
    ]
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO users (id, username, password_hash, display_name, handle) "
                "VALUES ('u1', 'lectora', 'x', 'Lectora', 'lectora')"
            ))
            for story_id, libro, creada, tocada in partidas:
                conn.execute(
                    text(
                        "INSERT INTO stories (id, user_id, character_id, phase, affinity, turn_count, summary, "
                        "created_at, updated_at) VALUES (:id, 'u1', :libro, 'conocerse', 20, 0, '', :c, :u)"
                    ),
                    {"id": story_id, "libro": libro, "c": creada, "u": tocada},
                )
        command.upgrade(config, "head")
        with engine.connect() as conn:
            filas = {r[0]: (r[1], r[2]) for r in conn.execute(text("SELECT id, status, archived_at, updated_at FROM stories"))}
            tocadas = dict(conn.execute(text("SELECT id, updated_at FROM stories")).all())
        activas = sorted(i for i, (estado, _) in filas.items() if estado == "activa")
        assert activas == ["a-nueva", "b-despues", "c-sola"]
        for story_id, (estado, archivada) in filas.items():
            assert archivada == (tocadas[story_id] if estado == "archivada" else None)

        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(text(
                "INSERT INTO stories (id, user_id, character_id, phase, affinity, turn_count, summary) "
                "VALUES ('otra', 'u1', 'lucia', 'conocerse', 20, 0, '')"
            ))
    finally:
        engine.dispose()


# --- Página de libro --------------------------------------------------------------------


def test_libro_predefinido_y_estado_del_lector(client):
    _, headers = _cuenta(client)
    libro = _libro(client, headers)
    assert (libro["origin"], libro["mode"], libro["author"], libro["isMine"], libro["isPublic"]) == (
        "psique", None, None, False, True
    )
    assert (libro["title"], libro["characterName"], libro["chapterCount"], libro["readCost"]) == (
        "Lucía Ferrer", "Lucía Ferrer", 5, settings.READ_COST
    )
    viewer = libro["viewer"]
    assert (viewer["status"], viewer["activeStoryId"], viewer["progress"]) == ("sin_empezar", None, None)
    assert viewer["primaryAction"] == {"kind": "leer", "cost": 0}
    assert (viewer["canReread"], viewer["canReview"], viewer["myReview"]) == (False, False, None)

    story = _empezar(client, headers)
    viewer = _libro(client, headers)["viewer"]
    assert (viewer["status"], viewer["activeStoryId"]) == ("leyendo", story["id"])
    assert viewer["primaryAction"] == {"kind": "continuar", "cost": 0}
    assert viewer["progress"]["chapterLocked"] is False and viewer["progress"]["phaseCount"] == 5
    assert (viewer["canReread"], viewer["canReview"]) == (True, True)

    with SessionLocal() as db:
        db.get(Story, story["id"]).phase = "desenlace"
        db.commit()
    assert _libro(client, headers)["viewer"]["status"] == "leido"


def test_libro_privado_ajeno_o_inexistente_es_404(client):
    _, autora = _cuenta(client)
    privado = _crear_libro(client, autora)
    assert _libro(client, autora, privado["characterId"])["isMine"] is True

    _, otra = _cuenta(client)
    for ruta in ("", "/history", "/recommended", "/reviews"):
        resp = client.get(f"/api/books/{privado['characterId']}{ruta}", headers=otra)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "No encontramos este libro."}
    assert client.get("/api/books/nadie", headers=otra).status_code == 404
    assert client.get("/api/books/lucia").status_code == 401


def test_libro_concepto_no_filtra_premisa_ni_perfil(client, llm_concepto):
    _, autora = _cuenta(client)
    resp = client.post(
        "/api/custom-stories", json={"mode": "concepto", "premise": PREMISA, "isPublic": True}, headers=autora
    )
    assert resp.status_code == 201, resp.text
    concepto = resp.json()

    _, lectora = _cuenta(client)
    resp = client.get(f"/api/books/{concepto['characterId']}", headers=lectora)
    assert resp.status_code == 200
    libro = resp.json()
    assert (libro["mode"], libro["characterName"], libro["title"]) == ("concepto", None, "La carta del faro")
    assert "premise" not in libro and "definition" not in libro
    for oculto in (PREMISA, "Elio Marín", SECRETO, "isla atlántica"):
        assert oculto not in resp.text


def test_recomendados_excluyen_privados_borrados_y_el_actual(client):
    _, autora = _cuenta(client)
    actual = _crear_libro(client, autora, isPublic=True, title="Actual", tone="Nostálgico y tierno")
    gemelo = _crear_libro(client, autora, isPublic=True, title="Gemelo", tone="nostalgico y TIERNO")
    privado_propio = _crear_libro(client, autora, title="Privado propio", tone="Nostálgico y tierno")
    borrado = _crear_libro(client, autora, isPublic=True, title="Borrado", tone="Nostálgico y tierno")
    client.delete(f"/api/custom-stories/{borrado['id']}", headers=autora)
    _, otra = _cuenta(client)
    privado_ajeno = _crear_libro(client, otra, title="Privado ajeno", tone="Nostálgico y tierno")

    resp = client.get(f"/api/books/{actual['characterId']}/recommended", headers=autora)
    assert resp.status_code == 200
    cartas = resp.json()
    ids = [c["id"] for c in cartas]
    assert len(cartas) <= 6
    # Mismo tono, modo y autor: la puntuación máxima.
    assert ids[0] == gemelo["characterId"]
    for fuera in (actual, privado_propio, borrado, privado_ajeno):
        assert fuera["characterId"] not in ids
    assert set(cartas[0]) == {"id", "origin", "mode", "title", "hook", "tone", "coverUrl", "author", "readers", "adult"}


# --- Reseñas ----------------------------------------------------------------------------


def _resenar(client, headers, book_id="lucia", **cuerpo):
    return client.put(f"/api/books/{book_id}/reviews/me", json={"rating": 4} | cuerpo, headers=headers)


def test_resenar_exige_partida_y_no_ser_el_autor(client):
    _, headers = _cuenta(client)
    resp = _resenar(client, headers)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "Solo puedes reseñar libros que hayas empezado.", "code": "not_started"}

    _, autora = _cuenta(client)
    libro = _crear_libro(client, autora, isPublic=True)
    _empezar(client, autora, libro["characterId"])
    resp = _resenar(client, autora, libro["characterId"])
    assert resp.status_code == 403
    assert resp.json() == {"detail": "No puedes reseñar tu propio libro.", "code": "own_book"}
    assert _libro(client, autora, libro["characterId"])["viewer"]["canReview"] is False


def test_una_resena_por_usuario_editar_y_borrar(client):
    _, headers = _cuenta(client)
    _empezar(client, headers, "mateo")

    resp = _resenar(client, headers, "mateo", rating=5, text="  Me encantó el guiso.  ")
    assert resp.status_code == 201, resp.text
    creada = resp.json()
    assert (creada["rating"], creada["text"], creada["isMine"]) == (5, "Me encantó el guiso.", True)
    assert isinstance(creada["id"], int)

    resp = _resenar(client, headers, "mateo", rating=3, text="   ")
    assert resp.status_code == 200
    assert (resp.json()["id"], resp.json()["rating"], resp.json()["text"]) == (creada["id"], 3, None)

    libro = _libro(client, headers, "mateo")
    assert libro["viewer"]["myReview"]["id"] == creada["id"]
    mias = [r for r in client.get("/api/books/mateo/reviews?limit=50", headers=headers).json()["items"] if r["isMine"]]
    assert len(mias) == 1

    assert _resenar(client, headers, "mateo", rating=6).status_code == 422
    assert _resenar(client, headers, "mateo", text="x" * 1001).status_code == 422

    assert client.delete("/api/books/mateo/reviews/me", headers=headers).status_code == 204
    assert client.delete("/api/books/mateo/reviews/me", headers=headers).status_code == 404
    assert _libro(client, headers, "mateo")["viewer"]["myReview"] is None


def test_resena_pasa_el_filtro_de_entrada(client):
    _, headers = _cuenta(client)
    _empezar(client, headers)
    resp = _resenar(client, headers, text="Buscaba sexo y no hay")
    assert resp.status_code == 422
    assert resp.json()["detail"] == [{"loc": ["body", "text"], "msg": EXPLICIT_INPUT_MESSAGE, "type": "content_policy"}]


def test_resenas_paginadas_recientes_primero_con_estadisticas(client):
    _, autora = _cuenta(client)
    libro = _crear_libro(client, autora, isPublic=True, title="Para reseñar")
    book_id = libro["characterId"]
    ids = []
    for nota in (5, 4, 2):
        _, headers = _cuenta(client)
        _empezar(client, headers, book_id)
        ids.append(_resenar(client, headers, book_id, rating=nota).json()["id"])

    pagina = client.get(f"/api/books/{book_id}/reviews", params={"limit": 2}, headers=autora).json()
    assert [r["id"] for r in pagina["items"]] == [ids[2], ids[1]]
    assert (pagina["limit"], pagina["offset"], pagina["nextOffset"]) == (2, 0, 2)
    assert pagina["items"][0]["isMine"] is False and pagina["items"][0]["author"]["handle"]

    resto = client.get(f"/api/books/{book_id}/reviews", params={"limit": 2, "offset": 2}, headers=autora).json()
    assert [r["id"] for r in resto["items"]] == [ids[0]]
    assert resto["nextOffset"] is None
    assert client.get(f"/api/books/{book_id}/reviews", params={"limit": 51}, headers=autora).status_code == 422

    assert _libro(client, autora, book_id)["stats"] == {"readers": 3, "ratingAverage": 3.7, "reviewCount": 3}
