"""Revisión de incidentes (cola dev) y apelación del usuario."""
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.story import ConductIncident
from app.models.user import User
from app.services import conduct_service
from scripts import cleanup
from tests.conftest import PASSWORD, register

EXPLICITO = "me gustan los pitos, enséñamelos"


def _cuenta(client, *, dev=False):
    body = register(client)
    if dev:
        with SessionLocal() as db:
            db.get(User, body["user"]["id"]).is_dev = True
            db.commit()
    return body["user"], {"Authorization": f"Bearer {body['access_token']}"}


def _cerrar(client, headers, libro="lucia", mensaje=EXPLICITO) -> str:
    story = client.post("/api/stories", json={"characterId": libro}, headers=headers).json()
    resp = client.post(f"/api/stories/{story['id']}/chat", json={"message": mensaje}, headers=headers)
    assert resp.status_code == 403 and resp.json()["code"] == "story_closed", resp.text
    return story["id"]


def _incidentes(client, headers):
    resp = client.get("/api/me/incidents", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_el_incidente_guarda_un_extracto_acotado_que_el_usuario_no_ve(client, fake_llm):
    _, headers = _cuenta(client)
    largo = EXPLICITO + " " + "x" * 600
    story_id = _cerrar(client, headers, mensaje=largo)

    with SessionLocal() as db:
        incidente = db.scalar(select(ConductIncident).where(ConductIncident.story_id == story_id))
        assert incidente.excerpt.startswith(EXPLICITO)
        assert len(incidente.excerpt) == settings.CONDUCT_EXCERPT_CHARS

    [mio] = _incidentes(client, headers)
    assert mio["storyId"] == story_id and mio["bookTitle"] and mio["appealStatus"] is None
    assert mio["counts"] is True
    assert "excerpt" not in mio and "rule" not in mio
    # En el export sí, que es suyo.
    export = client.get("/api/me/export", headers=headers).json()
    assert export["conductIncidents"][0]["excerpt"].startswith(EXPLICITO)


def test_apelar_una_vez_con_texto_opcional(client, fake_llm):
    _, headers = _cuenta(client)
    _cerrar(client, headers)
    [incidente] = _incidentes(client, headers)
    url = f"/api/me/incidents/{incidente['id']}/appeal"

    resp = client.post(url, json={"text": "Era una broma sobre silbatos, de verdad."}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["appealStatus"] == "pendiente" and resp.json()["appealedAt"]

    otra = client.post(url, json={}, headers=headers)
    assert otra.status_code == 409 and otra.json()["code"] == "already_appealed"


def test_apelacion_con_texto_explicito_da_422_sin_cerrar_nada(client, fake_llm):
    user, headers = _cuenta(client)
    _cerrar(client, headers)
    [incidente] = _incidentes(client, headers)

    resp = client.post(f"/api/me/incidents/{incidente['id']}/appeal", json={"text": EXPLICITO}, headers=headers)

    assert resp.status_code == 422 and resp.json()["code"] == "appeal_text_rejected"
    with SessionLocal() as db:
        assert db.scalar(select(ConductIncident).where(ConductIncident.id == incidente["id"])).appealed_at is None
        assert len(db.scalars(select(ConductIncident).where(ConductIncident.user_id == user["id"])).all()) == 1
    # Sigue pudiendo apelar con otro texto.
    ok = client.post(f"/api/me/incidents/{incidente['id']}/appeal", json={"text": "Fue un malentendido."}, headers=headers)
    assert ok.status_code == 200


def test_apelacion_de_texto_largo_y_de_incidente_ajeno(client, fake_llm):
    _, headers = _cuenta(client)
    _cerrar(client, headers)
    [incidente] = _incidentes(client, headers)
    url = f"/api/me/incidents/{incidente['id']}/appeal"
    assert client.post(url, json={"text": "a" * 501}, headers=headers).status_code == 422

    _, otros = _cuenta(client)
    ajena = client.post(url, json={}, headers=otros)
    assert ajena.status_code == 404


def test_la_cola_solo_para_devs(client):
    _, headers = _cuenta(client)
    assert client.get("/api/dev/incidents", headers=headers).status_code == 403
    assert client.post("/api/dev/incidents/1/accept", json={}, headers=headers).status_code == 403


def test_cola_filtros_y_detalle_con_extracto(client, fake_llm):
    _, usuario = _cuenta(client)
    _cerrar(client, usuario)
    [incidente] = _incidentes(client, usuario)
    _, dev = _cuenta(client, dev=True)

    sin_resolver = client.get("/api/dev/incidents?filter=sin_resolver&limit=100", headers=dev).json()
    fila = next(i for i in sin_resolver["items"] if i["id"] == incidente["id"])
    assert fila["excerpt"] is None and fila["hasExcerpt"] is True and fila["canReview"] is True

    pendientes = client.get("/api/dev/incidents?filter=pendientes&limit=100", headers=dev).json()
    assert incidente["id"] not in [i["id"] for i in pendientes["items"]]
    client.post(f"/api/me/incidents/{incidente['id']}/appeal", json={"text": "Revisadlo, por favor."}, headers=usuario)
    pendientes = client.get("/api/dev/incidents?filter=pendientes&limit=100", headers=dev).json()
    assert incidente["id"] in [i["id"] for i in pendientes["items"]]

    por_nivel = client.get("/api/dev/incidents?filter=todos&level=prohibido&limit=100", headers=dev).json()
    assert incidente["id"] not in [i["id"] for i in por_nivel["items"]]
    assert client.get("/api/dev/incidents?filter=otro", headers=dev).status_code == 422

    detalle = client.get(f"/api/dev/incidents/{incidente['id']}", headers=dev).json()
    assert detalle["excerpt"].startswith(EXPLICITO)
    assert detalle["appealText"] == "Revisadlo, por favor." and detalle["user"]["handle"]
    assert detalle["story"]["status"] == "cerrada"


def test_aceptar_reabre_saca_del_computo_y_levanta_la_restriccion(client, fake_llm):
    user, headers = _cuenta(client)
    ids = [_cerrar(client, headers, libro) for libro in ("lucia", "mateo", "lucia")]
    assert client.get("/api/me", headers=headers).json()["restrictedUntil"] is not None
    incidentes = _incidentes(client, headers)
    primero = next(i for i in incidentes if i["storyId"] == ids[0])
    client.post(f"/api/me/incidents/{primero['id']}/appeal", json={}, headers=headers)
    dev_user, dev = _cuenta(client, dev=True)

    resp = client.post(f"/api/dev/incidents/{primero['id']}/accept", json={"note": "Falso positivo."}, headers=dev)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["storyReopened"] is True
    assert body["incident"]["review"]["status"] == "aceptada"
    assert body["incident"]["review"]["reviewedBy"] == dev_user["handle"]
    assert body["incident"]["excerpt"] is None and body["incident"]["hasExcerpt"] is False
    assert client.get("/api/me", headers=headers).json()["restrictedUntil"] is None
    assert client.get(f"/api/stories/{ids[0]}", headers=headers).json()["status"] == "activa"
    mio = next(i for i in _incidentes(client, headers) if i["id"] == primero["id"])
    assert mio["appealStatus"] == "aceptada" and mio["counts"] is False and mio["reviewNote"] == "Falso positivo."
    with SessionLocal() as db:
        fila = db.get(ConductIncident, primero["id"])
        assert fila.reviewed_by_id == dev_user["id"] and fila.reviewed_at is not None and fila.excerpt is None

    otra = client.post(f"/api/dev/incidents/{primero['id']}/reject", json={}, headers=dev)
    assert otra.status_code == 409 and otra.json()["code"] == "already_resolved"
    # Resuelto, ya no se apela.
    assert client.post(f"/api/me/incidents/{primero['id']}/appeal", json={}, headers=headers).status_code == 409


def test_aceptar_no_reabre_si_ya_hay_otra_activa_del_libro(client, fake_llm):
    _, headers = _cuenta(client)
    cerrada = _cerrar(client, headers)
    nueva = client.post("/api/stories", json={"characterId": "lucia"}, headers=headers).json()
    assert nueva["status"] == "activa"
    [incidente] = _incidentes(client, headers)
    _, dev = _cuenta(client, dev=True)

    body = client.post(f"/api/dev/incidents/{incidente['id']}/accept", headers=dev).json()

    assert body["storyReopened"] is False and body["incident"]["review"]["status"] == "aceptada"
    assert client.get(f"/api/stories/{cerrada}", headers=headers).json()["status"] == "cerrada"


def test_rechazar_mantiene_cierre_y_restriccion(client, fake_llm):
    _, headers = _cuenta(client)
    for libro in ("lucia", "mateo", "lucia"):
        _cerrar(client, headers, libro)
    antes = client.get("/api/me", headers=headers).json()["restrictedUntil"]
    incidente = _incidentes(client, headers)[0]
    _, dev = _cuenta(client, dev=True)

    body = client.post(f"/api/dev/incidents/{incidente['id']}/reject", json={"note": "Explícito."}, headers=dev).json()

    assert body["storyReopened"] is False and body["incident"]["review"]["status"] == "rechazada"
    assert client.get("/api/me", headers=headers).json()["restrictedUntil"] == antes
    mio = next(i for i in _incidentes(client, headers) if i["id"] == incidente["id"])
    assert mio["appealStatus"] == "rechazada" and mio["counts"] is True


def test_un_dev_no_resuelve_lo_suyo_salvo_en_desarrollo(client, fake_llm, monkeypatch):
    # El .env local puede traer development: se fija explícitamente.
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    _, dev = _cuenta(client, dev=True)
    _cerrar(client, dev)
    [incidente] = _incidentes(client, dev)

    detalle = client.get(f"/api/dev/incidents/{incidente['id']}", headers=dev).json()
    assert detalle["canReview"] is False
    resp = client.post(f"/api/dev/incidents/{incidente['id']}/accept", headers=dev)
    assert resp.status_code == 403 and resp.json()["code"] == "own_incident"

    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    assert client.post(f"/api/dev/incidents/{incidente['id']}/accept", headers=dev).status_code == 200


def test_el_extracto_caduca_y_cleanup_lo_borra(client, fake_llm):
    _, headers = _cuenta(client)
    story_id = _cerrar(client, headers)
    _, dev = _cuenta(client, dev=True)
    with SessionLocal() as db:
        incidente = db.scalar(select(ConductIncident).where(ConductIncident.story_id == story_id))
        incidente.created_at -= timedelta(days=settings.CONDUCT_EXCERPT_DAYS + 1)
        db.commit()
        incidente_id = incidente.id

    detalle = client.get(f"/api/dev/incidents/{incidente_id}", headers=dev).json()
    assert detalle["excerpt"] is None and detalle["hasExcerpt"] is False

    with SessionLocal() as db:

        report = cleanup.run(db, Path(settings.MEDIA_DIR), apply=True)
        assert report.incident_excerpts >= 1
    with SessionLocal() as db:
        assert db.get(ConductIncident, incidente_id).excerpt is None


def test_recalculo_solo_acorta(client, fake_llm):
    user, headers = _cuenta(client)
    with SessionLocal() as db:
        cuenta = db.get(User, user["id"])
        manual = conduct_service.now() + timedelta(days=2)
        cuenta.restricted_until = manual
        db.commit()
        # Sin incidentes que la justifiquen, el recálculo la levanta.
        assert conduct_service.recompute_restriction(db, cuenta) is None


def test_borrar_cuenta_dev_anonimiza_sus_revisiones(client, fake_llm):
    _, headers = _cuenta(client)
    _cerrar(client, headers)
    [incidente] = _incidentes(client, headers)
    body = register(client)
    with SessionLocal() as db:
        db.get(User, body["user"]["id"]).is_dev = True
        db.commit()
    dev = {"Authorization": f"Bearer {body['access_token']}"}
    client.post(f"/api/dev/incidents/{incidente['id']}/reject", headers=dev)


    borrar = client.request("DELETE", "/api/me", json={"password": PASSWORD, "confirmation": "BORRAR"}, headers=dev)
    assert borrar.status_code == 204
    with SessionLocal() as db:
        fila = db.get(ConductIncident, incidente["id"])
        assert fila.review_status == "rechazada" and fila.reviewed_by_id is None and fila.reviewed_by_handle is None
