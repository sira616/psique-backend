"""Aceptación de términos y privacidad: obligatoria al registrarse y otra vez si cambia la versión."""
import uuid

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.user import User
from tests.conftest import PASSWORD, TERMS, register


def _registro(client, **extra):
    return client.post("/api/auth/register", json={"username": f"t{uuid.uuid4().hex[:12]}", "password": PASSWORD, **extra})


def test_registro_sin_aceptar_se_rechaza(client):
    for extra in ({}, {"accept_terms": True}, {"min_age_confirmed": True}, {"accept_terms": False, "min_age_confirmed": True}):
        resp = _registro(client, **extra)
        assert resp.status_code == 422, extra
        assert "términos" in resp.json()["detail"] and str(settings.MIN_AGE) in resp.json()["detail"]


def test_registro_guarda_version_y_fecha(client):
    resp = _registro(client, **TERMS)
    assert resp.status_code == 201
    user = resp.json()["user"]
    assert user["termsAccepted"] is True and user["termsVersion"] == settings.TERMS_VERSION
    with SessionLocal() as db:
        fila = db.get(User, user["id"])
        assert fila.terms_version == settings.TERMS_VERSION and fila.terms_accepted_at is not None


def test_version_nueva_obliga_a_aceptar_otra_vez(client, monkeypatch):
    body = register(client)
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    monkeypatch.setattr(settings, "TERMS_VERSION", "2099-01-01")

    me = client.get("/api/me", headers=headers).json()
    assert me["termsAccepted"] is False and me["termsVersion"] == "2099-01-01"

    vieja = client.post("/api/me/accept-terms", json={"version": "2026-09-18", "confirm": True}, headers=headers)
    assert vieja.status_code == 409 and vieja.json()["code"] == "terms_outdated"
    assert client.post("/api/me/accept-terms", json={"version": "2099-01-01"}, headers=headers).status_code == 422

    ok = client.post("/api/me/accept-terms", json={"version": "2099-01-01", "confirm": True}, headers=headers)
    assert ok.status_code == 200 and ok.json()["termsAccepted"] is True
    assert client.get("/api/me", headers=headers).json()["termsAccepted"] is True


def test_cuentas_anteriores_sin_aceptar(client):
    body = register(client)
    with SessionLocal() as db:
        fila = db.get(User, body["user"]["id"])
        fila.terms_version = None
        fila.terms_accepted_at = None
        db.commit()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    assert client.get("/api/me", headers=headers).json()["termsAccepted"] is False
