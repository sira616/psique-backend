"""Perfil público: handle, validación por campo y privacidad de las estanterías."""
import uuid

import pytest

from app.story.content_policy import EXPLICIT_INPUT_MESSAGE
from tests.conftest import PASSWORD, register
from tests.test_custom_stories import _definida


def _cuenta(client, username=None) -> tuple[dict, dict]:
    sesion = register(client, username)
    return sesion["user"], {"Authorization": f"Bearer {sesion['access_token']}"}


def _patch(client, headers, body):
    return client.patch("/api/me/profile", json=body, headers=headers)


def _error(resp, campo) -> dict:
    errores = resp.json()["detail"]
    return next(e for e in errores if e["loc"] == ["body", campo])


def test_registrar_asigna_handle_derivado_y_resuelve_colisiones(client):
    sufijo = uuid.uuid4().hex[:8]
    primera, _ = _cuenta(client, f"ana.{sufijo}")
    segunda, _ = _cuenta(client, f"ana-{sufijo}")
    assert primera["handle"] == f"ana_{sufijo}"
    assert segunda["handle"] == f"ana_{sufijo}_2"


def test_perfil_propio_por_defecto(client):
    usuario, headers = _cuenta(client)
    assert client.get("/api/me/profile", headers=headers).json() == {
        "handle": usuario["username"],
        "displayName": usuario["displayName"],
        "bio": None,
        "link": None,
        "avatarUrl": None,
        "bannerUrl": None,
        "showPublished": True,
        "showReading": True,
    }
    assert client.get("/api/me/profile").status_code == 401


def test_cambiar_handle_no_cambia_el_login(client):
    usuario, headers = _cuenta(client)
    nuevo = f"Nuevo_{uuid.uuid4().hex[:8]}"
    resp = _patch(client, headers, {"handle": nuevo, "displayName": "  Lucía   Pérez ", "bio": "Leo de noche.", "link": "https://ejemplo.com/yo"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["handle"], body["displayName"], body["bio"], body["link"]) == (nuevo.lower(), "Lucía Pérez", "Leo de noche.", "https://ejemplo.com/yo")

    login = client.post("/api/auth/login", json={"login": usuario["username"], "password": PASSWORD})
    assert login.status_code == 200 and login.json()["user"]["handle"] == nuevo.lower()

    otra = _cuenta(client)[1]
    assert client.get(f"/api/profiles/{nuevo}", headers=otra).json()["displayName"] == "Lucía Pérez"
    assert client.get(f"/api/profiles/{usuario['username']}", headers=otra).status_code == 404
    # Vaciar la bio y el enlace; lo que no viene no cambia.
    body = _patch(client, headers, {"bio": None, "link": ""}).json()
    assert (body["bio"], body["link"], body["displayName"]) == (None, None, "Lucía Pérez")


def test_handle_duplicado_es_409_con_campo(client):
    ocupado, _ = _cuenta(client)
    _, headers = _cuenta(client)
    resp = _patch(client, headers, {"handle": ocupado["handle"].upper()})
    assert resp.status_code == 409
    assert _error(resp, "handle")["type"] == "handle_taken"
    # Poner el suyo propio no choca consigo mismo.
    propio = client.get("/api/me/profile", headers=headers).json()["handle"]
    assert _patch(client, headers, {"handle": propio}).status_code == 200


@pytest.mark.parametrize("handle", ["ab", "a" * 31, "con espacio", "con-guion", "ñandú", "punto.no", ""])
def test_formato_de_handle(client, handle):
    resp = _patch(client, _cuenta(client)[1], {"handle": handle})
    assert resp.status_code == 422
    assert _error(resp, "handle")["type"] == "handle_format"


@pytest.mark.parametrize(
    "link",
    ["javascript:alert(1)", "ftp://ejemplo.com", "https://", "ejemplo.com", "http://localhost",
     "https://banco.es@otra.web", "https://ejemplo.com/con espacio", "https://" + "a" * 200 + ".com"],
)
def test_link_invalido(client, link):
    resp = _patch(client, _cuenta(client)[1], {"link": link})
    assert resp.status_code == 422
    assert _error(resp, "link")["type"] in ("link_format", "too_long")


def test_bio_de_mas_de_280_y_campos_no_vaciables(client):
    headers = _cuenta(client)[1]
    assert _patch(client, headers, {"bio": "a" * 280}).status_code == 200
    resp = _patch(client, headers, {"bio": "a" * 281, "displayName": None, "showReading": None})
    assert resp.status_code == 422
    assert _error(resp, "bio")["type"] == "too_long"
    assert _error(resp, "displayName")["type"] == "required"
    assert _error(resp, "showReading")["type"] == "required"
    assert _patch(client, headers, {"isDev": True}).status_code == 422


def test_filtro_de_contenido_en_nombre_y_bio(client):
    headers = _cuenta(client)[1]
    resp = _patch(client, headers, {"displayName": "Todo erótico", "bio": "Busco sexo"})
    assert resp.status_code == 422
    assert _error(resp, "bio")["msg"] == EXPLICIT_INPUT_MESSAGE
    assert _error(resp, "displayName")["type"] == "content_policy"
    assert client.get("/api/me/profile", headers=headers).json()["bio"] is None


def _jugar_y_hablar(client, headers, character_id):
    story = client.post("/api/stories", json={"characterId": character_id}, headers=headers).json()
    client.post(f"/api/stories/{story['id']}/chat", json={"message": "Un mensaje muy privado"}, headers=headers)
    return story


def test_estanterias_publicadas_y_leyendo(client, fake_llm):
    duena, headers = _cuenta(client)
    publica = client.post("/api/custom-stories", json=_definida(isPublic=True), headers=headers).json()
    privada = client.post("/api/custom-stories", json=_definida(title="Mi cuaderno secreto"), headers=headers).json()
    _jugar_y_hablar(client, headers, "lucia")
    _jugar_y_hablar(client, headers, privada["characterId"])

    otra = _cuenta(client)[1]
    resp = client.get(f"/api/profiles/{duena['handle']}", headers=otra)
    assert resp.status_code == 200
    perfil = resp.json()
    assert perfil["isOwner"] is False
    assert [c["id"] for c in perfil["shelves"]["published"]["items"]] == [publica["id"]]

    leyendo = perfil["shelves"]["reading"]["items"]
    # La partida con la historia privada no sale para otros: su título no está publicado.
    assert [i["characterId"] for i in leyendo] == ["lucia"]
    assert set(leyendo[0]) == {"characterId", "origin", "mode", "title", "characterName", "progress", "updatedAt"}
    assert leyendo[0]["progress"]["phase"] == "conocerse"
    for filtrado in ("Un mensaje muy privado", "Me alegra que hayas vuelto", "messages", "affinity", "Mi cuaderno secreto"):
        assert filtrado not in resp.text

    propio = client.get(f"/api/profiles/{duena['handle']}", headers=headers).json()
    assert propio["isOwner"] is True
    assert {i["title"] for i in propio["shelves"]["reading"]["items"]} == {"Lucía Ferrer", "Mi cuaderno secreto"}


def test_estanterias_ocultas_para_otros_pero_no_para_la_duena(client, fake_llm):
    duena, headers = _cuenta(client)
    client.post("/api/custom-stories", json=_definida(isPublic=True), headers=headers)
    _jugar_y_hablar(client, headers, "lucia")
    assert _patch(client, headers, {"showPublished": False, "showReading": False}).status_code == 200

    ajeno = client.get(f"/api/profiles/{duena['handle']}", headers=_cuenta(client)[1]).json()
    assert ajeno["shelves"] == {
        "published": {"items": None, "visibleToOthers": False},
        "reading": {"items": None, "visibleToOthers": False},
    }
    propio = client.get(f"/api/profiles/{duena['handle']}", headers=headers).json()
    assert len(propio["shelves"]["published"]["items"]) == 1
    assert len(propio["shelves"]["reading"]["items"]) == 1
    assert propio["shelves"]["published"]["visibleToOthers"] is False


def test_perfil_inexistente_y_sin_sesion(client):
    headers = _cuenta(client)[1]
    assert client.get("/api/profiles/no_existe_nadie_asi", headers=headers).status_code == 404
    assert client.get("/api/profiles/loquesea").status_code == 401
