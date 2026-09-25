"""Subida de imágenes: lo común al avatar y al banner del perfil y a la portada de una
historia propia.

Vive fuera de los routers porque es lo que protege de un fichero de 1 GB: duplicarlo sería
duplicar esa protección y arriesgarse a que una copia se quede atrás.
"""
from fastapi import HTTPException, Request, status
from starlette.datastructures import UploadFile

from app.core.config import settings
from app.core.rate_limit import RateLimiter
from app.services.profile_service import field_error

# Re-codificar imágenes es CPU nuestra: más holgado que el login, pero con tope. Uno solo
# para todas las subidas: el coste es el mismo venga de donde venga.
upload_rate_limiter = RateLimiter(settings.RATE_LIMIT_MAX_REQUESTS * 2, settings.RATE_LIMIT_WINDOW_SECONDS)

# Lo que añade el multipart alrededor del fichero (boundary y cabeceras de la parte).
_MULTIPART_MARGIN = 16 * 1024

UPLOAD_DOC = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {"file": {"type": "string", "format": "binary"}},
                }
            }
        },
    }
}


def file_error(status_code: int, msg: str, type_: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=[field_error("file", msg, type_)])


async def read_upload(request: Request, max_bytes: int) -> bytes:
    """Lee el campo `file` sin aceptar más de `max_bytes`.

    El cuerpo se lee a mano y no con `UploadFile = File()` porque FastAPI parsea el
    formulario entero antes de llamar a la ruta: un fichero de 1 GB llegaría a disco antes
    de poder rechazarlo. Aquí se corta por Content-Length, que el servidor HTTP no deja
    exceder, antes de leer nada.
    """
    too_big = file_error(
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        f"La imagen no puede pasar de {max_bytes // (1024 * 1024)} MB.",
        "file_too_large",
    )
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise file_error(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Envía la imagen como multipart/form-data.", "multipart_required"
        )
    try:
        length = int(request.headers["content-length"])
    except (KeyError, ValueError):
        raise file_error(status.HTTP_411_LENGTH_REQUIRED, "Falta la cabecera Content-Length.", "length_required")
    if length > max_bytes + _MULTIPART_MARGIN:
        raise too_big

    form = await request.form(max_files=1, max_fields=1)
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise file_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Falta la imagen en el campo `file`.", "missing")
        data = await upload.read(max_bytes + 1)
    finally:
        await form.close()
    if len(data) > max_bytes:
        raise too_big
    if not data:
        raise file_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "La imagen está vacía.", "missing")
    return data
