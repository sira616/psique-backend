"""Avatar y banner: tipo real, EXIF fuera, límites y limpieza de ficheros."""
import os
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.core.config import settings
from tests.conftest import MEDIA_DIR, auth_headers

GPS_TEXTO = "GPS 40.4168 -3.7038 casa"


def _imagen(fmt="JPEG", size=(800, 600), mode="RGB", exif=True) -> bytes:
    color = {"1": 1, "RGBA": (0, 128, 128, 100)}.get(mode, "teal")
    img = Image.new(mode, size, color=color)
    out = BytesIO()
    kwargs = {}
    if exif and fmt == "JPEG":
        datos = Image.Exif()
        datos[0x010F] = "MarcaDeCamara"  # Make
        datos[0x010E] = GPS_TEXTO  # ImageDescription
        kwargs["exif"] = datos.tobytes()
    img.save(out, format=fmt, **kwargs)
    return out.getvalue()


def _subir(client, headers, data: bytes, ruta="avatar", nombre="foto.jpg", tipo="image/jpeg"):
    return client.post(f"/api/me/{ruta}", files={"file": (nombre, data, tipo)}, headers=headers)


def _fichero(url: str) -> Path:
    assert url.startswith("/media/")
    return Path(MEDIA_DIR) / url.removeprefix("/media/")


def test_subir_avatar_recodifica_y_quita_exif(client):
    headers = auth_headers(client)
    original = _imagen(size=(1200, 900))
    assert b"MarcaDeCamara" in original

    resp = _subir(client, headers, original)
    assert resp.status_code == 200, resp.text
    url = resp.json()["avatarUrl"]
    assert url.startswith("/media/avatars/") and url.endswith(".webp")
    assert _fichero(url).is_file()

    servida = client.get(url)
    assert servida.status_code == 200
    assert servida.headers["content-type"] == "image/webp"
    assert b"MarcaDeCamara" not in servida.content and GPS_TEXTO.encode() not in servida.content
    with Image.open(BytesIO(servida.content)) as img:
        assert img.format == "WEBP"
        assert max(img.size) == 512
        assert len(img.getexif()) == 0 and "exif" not in img.info
    assert client.get("/api/me/profile", headers=headers).json()["avatarUrl"] == url


def test_la_orientacion_exif_se_aplica_antes_de_quitarlo(client):
    img = Image.new("RGB", (40, 20), "white")
    datos = Image.Exif()
    datos[0x0112] = 6  # girar 90°
    out = BytesIO()
    img.save(out, format="JPEG", exif=datos.tobytes())
    url = _subir(client, auth_headers(client), out.getvalue()).json()["avatarUrl"]
    with Image.open(_fichero(url)) as guardada:
        assert guardada.size == (20, 40)


@pytest.mark.parametrize(
    "data, nombre, tipo",
    [
        (b"esto no es una imagen, solo texto" * 10, "foto.jpg", "image/jpeg"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "foto.png", "image/png"),
        (_imagen("GIF", exif=False), "foto.png", "image/png"),
        (_imagen()[:200], "cortada.jpg", "image/jpeg"),
    ],
)
def test_tipo_falso_con_extension_valida_es_rechazado(client, data, nombre, tipo):
    headers = auth_headers(client)
    antes = set(os.listdir(MEDIA_DIR))
    resp = _subir(client, headers, data, nombre=nombre, tipo=tipo)
    assert resp.status_code == 415, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "file"]
    assert client.get("/api/me/profile", headers=headers).json()["avatarUrl"] is None
    assert set(os.listdir(MEDIA_DIR)) == antes


def test_png_y_webp_con_transparencia_se_aceptan(client):
    headers = auth_headers(client)
    for fmt in ("PNG", "WEBP"):
        resp = _subir(client, headers, _imagen(fmt, (300, 300), "RGBA", exif=False), nombre="x.bin", tipo="application/octet-stream")
        assert resp.status_code == 200, resp.text
        with Image.open(_fichero(resp.json()["avatarUrl"])) as img:
            assert img.mode == "RGBA"


def test_limite_de_tamanio(client, monkeypatch):
    headers = auth_headers(client)
    # Un cuerpo que ya por Content-Length pasa de 2 MB: se rechaza sin parsear el formulario.
    resp = _subir(client, headers, b"\xff" * (2 * 1024 * 1024 + 20 * 1024))
    assert resp.status_code == 413
    assert resp.json()["detail"][0]["type"] == "file_too_large"

    # Dentro del margen del multipart pero por encima del límite: se corta al leer.
    monkeypatch.setattr(settings, "AVATAR_MAX_BYTES", 5_000)
    resp = _subir(client, headers, _imagen(exif=False) + b"\0" * 6_000)
    assert resp.status_code == 413


def test_bomba_de_descompresion(client):
    # 6000x6000 en blanco y negro: pocos KB en PNG, 36 Mpx al decodificar.
    bomba = _imagen("PNG", (6000, 6000), "1", exif=False)
    assert len(bomba) < 200_000
    resp = _subir(client, auth_headers(client), bomba, nombre="bomba.png", tipo="image/png")
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["type"] == "image_too_large"


def test_reemplazar_y_quitar_borran_el_fichero_anterior(client):
    headers = auth_headers(client)
    primera = _fichero(_subir(client, headers, _imagen()).json()["avatarUrl"])
    segunda = _fichero(_subir(client, headers, _imagen(size=(100, 100))).json()["avatarUrl"])
    assert primera != segunda
    assert not primera.exists() and segunda.exists()

    assert client.delete("/api/me/avatar", headers=headers).status_code == 204
    assert not segunda.exists()
    assert client.get("/api/me/profile", headers=headers).json()["avatarUrl"] is None
    assert client.delete("/api/me/avatar", headers=headers).status_code == 204


def test_banner_limita_a_1600_y_es_independiente_del_avatar(client):
    headers = auth_headers(client)
    avatar = _subir(client, headers, _imagen()).json()["avatarUrl"]
    resp = _subir(client, headers, _imagen(size=(3000, 1000)), ruta="banner")
    assert resp.status_code == 200
    body = resp.json()
    assert body["avatarUrl"] == avatar and body["bannerUrl"].startswith("/media/banners/")
    with Image.open(_fichero(body["bannerUrl"])) as img:
        assert img.size == (1600, 533)
    assert client.delete("/api/me/banner", headers=headers).status_code == 204
    assert _fichero(avatar).exists()


def test_peticiones_mal_formadas(client):
    headers = auth_headers(client)
    assert client.post("/api/me/avatar", json={"file": "x"}, headers=headers).status_code == 415
    resp = client.post("/api/me/avatar", files={"otro": ("a.jpg", _imagen(), "image/jpeg")}, headers=headers)
    assert resp.status_code == 422 and resp.json()["detail"][0]["type"] == "missing"
    assert client.post("/api/me/avatar", files={"file": ("a.jpg", _imagen(), "image/jpeg")}).status_code == 401
