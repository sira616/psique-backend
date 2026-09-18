"""Auth local básica: registro, login, tokens y cierre de sesión."""
import uuid

import jwt

from app.core.config import settings
from tests.conftest import PASSWORD, TERMS, register


def _name() -> str:
    return f"u{uuid.uuid4().hex[:12]}"


def test_el_registro_ya_devuelve_la_sesion(client):
    body = register(client)
    me = client.get("/api/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["username"] == body["user"]["username"]


def test_el_usuario_no_distingue_mayusculas(client):
    name = _name()
    register(client, name.upper())
    resp = client.post("/api/auth/register", json={"username": name, "password": PASSWORD, **TERMS})
    assert resp.status_code == 409


def test_contrasena_floja_es_422(client):
    resp = client.post("/api/auth/register", json={"username": _name(), "password": "todominusculas123", **TERMS})
    assert resp.status_code == 422


def test_login_y_mensaje_unico(client):
    name = _name()
    register(client, name)
    assert client.post("/api/auth/login", json={"login": name, "password": PASSWORD}).status_code == 200
    mala = client.post("/api/auth/login", json={"login": name, "password": "OtraCosa123456"})
    inexistente = client.post("/api/auth/login", json={"login": _name(), "password": PASSWORD})
    assert mala.status_code == inexistente.status_code == 401
    assert mala.json()["detail"] == inexistente.json()["detail"]


def test_sin_credencial_es_401(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/characters").status_code == 401


def _register_raw(client):
    return client.post("/api/auth/register", json={"username": _name(), "password": PASSWORD, **TERMS})


def _cookie(resp) -> str:
    return resp.cookies["psique_refresh"]


def _refresh(client, token: str | None):
    """Petición con esa cookie y ninguna otra: el cliente de sesión acumula las de todos."""
    client.cookies.clear()
    headers = {"Cookie": f"psique_refresh={token}"} if token else {}
    return client.post("/api/auth/refresh", headers=headers)


def test_el_refresh_va_en_cookie_httponly_y_no_en_el_cuerpo(client):
    resp = _register_raw(client)
    assert "refresh_token" not in resp.json()
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/api/auth" in cookie


def test_un_refresh_no_sirve_como_access(client):
    resp = _register_raw(client)
    me = client.get("/api/me", headers={"Authorization": f"Bearer {_cookie(resp)}"})
    assert me.status_code == 401


def test_refrescar_rota_devuelve_usuario_y_anula_el_anterior(client):
    resp = _register_raw(client)
    primero = _cookie(resp)
    nuevo = _refresh(client, primero)
    assert nuevo.status_code == 200
    assert nuevo.json()["user"]["username"] == resp.json()["user"]["username"]
    assert _cookie(nuevo) != primero
    assert _refresh(client, primero).status_code == 401


def test_sin_cookie_el_refresh_es_401(client):
    assert _refresh(client, None).status_code == 401


def test_logout_revoca_el_refresh_y_borra_la_cookie(client):
    token = _cookie(_register_raw(client))
    client.cookies.clear()
    out = client.post("/api/auth/logout", headers={"Cookie": f"psique_refresh={token}"})
    assert out.status_code == 204
    assert "psique_refresh=" in out.headers["set-cookie"]
    assert _refresh(client, token).status_code == 401


def test_la_api_no_crea_cuentas_dev(client):
    resp = client.post(
        "/api/auth/register", json={"username": _name(), "password": PASSWORD, "is_dev": True, **TERMS}
    )
    assert resp.json()["user"]["isDev"] is False


def test_los_tokens_llevan_el_tipo(client):
    body = register(client)
    access = jwt.decode(body["access_token"], settings.JWT_SECRET, algorithms=["HS256"])
    assert access["type"] == "access"
