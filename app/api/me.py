from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import RateLimiter
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.auth import AuthUserOut
from app.schemas.profile import MyProfileOut, ProfilePatchIn
from app.services import media_service, profile_service
from app.services.media_service import AVATAR, BANNER, ImageKind, ImageRejectedError
from app.services.profile_service import ProfileError, field_error

router = APIRouter(prefix="/api/me", tags=["me"])

# Re-codificar imágenes es CPU nuestra: más holgado que el login, pero con tope.
_upload_rate_limiter = RateLimiter(settings.RATE_LIMIT_MAX_REQUESTS * 2, settings.RATE_LIMIT_WINDOW_SECONDS)

# Lo que añade el multipart alrededor del fichero (boundary y cabeceras de la parte).
_MULTIPART_MARGIN = 16 * 1024

_UPLOAD_DOC = {
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


@router.get("", response_model=AuthUserOut)
def get_me(user: User = Depends(get_current_user)):
    return AuthUserOut.from_user(user)


@router.get("/profile", response_model=MyProfileOut)
def get_my_profile(user: User = Depends(get_current_user)):
    return profile_service.my_profile_out(user)


@router.patch("/profile", response_model=MyProfileOut)
def update_my_profile(
    payload: ProfilePatchIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    try:
        profile_service.update(db, user, payload)
    except ProfileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.errors)
    return profile_service.my_profile_out(user)


def _file_error(status_code: int, msg: str, type_: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail=[field_error("file", msg, type_)])


async def _read_upload(request: Request, max_bytes: int) -> bytes:
    """Lee el campo `file` sin aceptar más de `max_bytes`.

    El cuerpo se lee a mano y no con `UploadFile = File()` porque FastAPI parsea el
    formulario entero antes de llamar a la ruta: un fichero de 1 GB llegaría a disco antes
    de poder rechazarlo. Aquí se corta por Content-Length, que el servidor HTTP no deja
    exceder, antes de leer nada.
    """
    too_big = _file_error(
        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        f"La imagen no puede pasar de {max_bytes // (1024 * 1024)} MB.",
        "file_too_large",
    )
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        raise _file_error(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Envía la imagen como multipart/form-data.", "multipart_required"
        )
    try:
        length = int(request.headers["content-length"])
    except (KeyError, ValueError):
        raise _file_error(status.HTTP_411_LENGTH_REQUIRED, "Falta la cabecera Content-Length.", "length_required")
    if length > max_bytes + _MULTIPART_MARGIN:
        raise too_big

    form = await request.form(max_files=1, max_fields=1)
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise _file_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "Falta la imagen en el campo `file`.", "missing")
        data = await upload.read(max_bytes + 1)
    finally:
        await form.close()
    if len(data) > max_bytes:
        raise too_big
    if not data:
        raise _file_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "La imagen está vacía.", "missing")
    return data


async def _upload(request: Request, kind: ImageKind, db: Session, user: User) -> MyProfileOut:
    data = await _read_upload(request, kind.max_bytes)
    try:
        # Decodificar y re-codificar bloquea: fuera del bucle de eventos.
        await run_in_threadpool(media_service.store, db, user, kind, data)
    except ImageRejectedError as exc:
        raise _file_error(exc.status_code, str(exc), exc.code)
    return profile_service.my_profile_out(user)


@router.post(
    "/avatar", response_model=MyProfileOut, openapi_extra=_UPLOAD_DOC, dependencies=[Depends(_upload_rate_limiter)]
)
async def upload_avatar(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return await _upload(request, AVATAR, db, user)


@router.post(
    "/banner", response_model=MyProfileOut, openapi_extra=_UPLOAD_DOC, dependencies=[Depends(_upload_rate_limiter)]
)
async def upload_banner(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return await _upload(request, BANNER, db, user)


@router.delete("/avatar", status_code=status.HTTP_204_NO_CONTENT)
def delete_avatar(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    media_service.remove(db, user, AVATAR)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/banner", status_code=status.HTTP_204_NO_CONTENT)
def delete_banner(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    media_service.remove(db, user, BANNER)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
