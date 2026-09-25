from typing import Literal

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.api._uploads import UPLOAD_DOC, file_error, read_upload, upload_rate_limiter
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limit import RateLimiter
from app.core.security import get_current_user
from app.models.user import User
from app.schemas.auth import AuthUserOut
from app.schemas.profile import MyProfileOut, ProfilePatchIn
from app.api.auth import clear_refresh_cookie
from app.core.passwords import MAX_LENGTH
from app.schemas.conduct import AppealIn, MyIncidentOut
from app.services import (
    account_service, conduct_service, incident_service, media_service, profile_service, usage_service,
)
from app.story import moderation
from app.services.media_service import AVATAR, BANNER, ImageKind, ImageRejectedError
from app.services.profile_service import ProfileError

router = APIRouter(prefix="/api/me", tags=["me"])

# Exportar recorre toda la cuenta; borrar comprueba la contraseña. Los dos, al ritmo del login.
_export_rate_limiter = RateLimiter(settings.RATE_LIMIT_MAX_REQUESTS, settings.RATE_LIMIT_WINDOW_SECONDS)
_delete_rate_limiter = RateLimiter(settings.RATE_LIMIT_MAX_REQUESTS, settings.RATE_LIMIT_WINDOW_SECONDS)
# Una apelación por incidente ya limita, pero el texto pasa por el moderador LLM: con tope.
_appeal_rate_limiter = RateLimiter(settings.RATE_LIMIT_MAX_REQUESTS, settings.RATE_LIMIT_WINDOW_SECONDS)


@router.get("", response_model=AuthUserOut)
def get_me(user: User = Depends(get_current_user)):
    return AuthUserOut.from_user(user)


class UsageOut(BaseModel):
    turnsUsed: int
    # null con cupo ilimitado (cuentas dev con DEV_UNLIMITED_TURNS).
    turnsLimit: int | None
    turnsRemaining: int | None
    unlimited: bool
    resetsAt: datetime


@router.get("/usage", response_model=UsageOut)
def get_usage(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return usage_service.usage_out(db, user)


@router.get("/export", dependencies=[Depends(_export_rate_limiter)])
def export_account(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    data = account_service.export_data(db, user)
    filename = f"psique-datos-{user.handle}-{usage_service.local_today().isoformat()}.json"
    return JSONResponse(
        content=jsonable_encoder(data),
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


class AccountDeleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=MAX_LENGTH)
    # Escrito a mano por la persona: un clic por error no borra nada.
    confirmation: Literal["BORRAR"]


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_delete_rate_limiter)])
def delete_account(payload: AccountDeleteIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    try:
        account_service.delete_account(db, user, payload.password)
    except account_service.InvalidPasswordError:
        # 403 y no 401: el cliente trata un 401 como sesión caducada y refrescaría.
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": "La contraseña no es correcta.", "code": "invalid_password"},
        )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_refresh_cookie(response)
    return response


class AdultConfirmationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Tiene que ser `true` literal: la confirmación es un acto explícito, no un default.
    confirm: Literal[True]


@router.post("/adult-confirmation", response_model=AuthUserOut)
def confirm_adult(payload: AdultConfirmationIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Declaración de mayoría de edad para abrir libros +18. No es una verificación."""
    return AuthUserOut.from_user(conduct_service.confirm_adult(db, user))


@router.delete("/adult-confirmation", response_model=AuthUserOut)
def revoke_adult(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return AuthUserOut.from_user(conduct_service.revoke_adult(db, user))


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


async def _upload(request: Request, kind: ImageKind, db: Session, user: User) -> MyProfileOut:
    data = await read_upload(request, kind.max_bytes)
    try:
        # Decodificar y re-codificar bloquea: fuera del bucle de eventos.
        await run_in_threadpool(media_service.store, db, user, kind, data)
    except ImageRejectedError as exc:
        raise file_error(exc.status_code, str(exc), exc.code)
    return profile_service.my_profile_out(user)


@router.post(
    "/avatar", response_model=MyProfileOut, openapi_extra=UPLOAD_DOC, dependencies=[Depends(upload_rate_limiter)]
)
async def upload_avatar(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return await _upload(request, AVATAR, db, user)


@router.post(
    "/banner", response_model=MyProfileOut, openapi_extra=UPLOAD_DOC, dependencies=[Depends(upload_rate_limiter)]
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


class TermsAcceptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # La versión que se le enseñó: si entretanto cambió, tiene que leer la nueva.
    version: str = Field(min_length=1, max_length=20)
    confirm: Literal[True]


@router.post("/accept-terms", response_model=AuthUserOut)
def accept_terms(payload: TermsAcceptIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if payload.version != settings.TERMS_VERSION:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": "Los términos han cambiado mientras los leías. Recarga para ver la versión vigente.",
                "code": "terms_outdated",
                "termsVersion": settings.TERMS_VERSION,
            },
        )
    return AuthUserOut.from_user(account_service.accept_terms(db, user))


@router.get("/incidents", response_model=list[MyIncidentOut])
def my_incidents(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Partidas cerradas por la política y el estado de su apelación. Sin regla ni extracto."""
    return incident_service.my_incidents(db, user)


APPEAL_TEXT_REJECTED = (
    "No podemos enviar la apelación con ese texto. Cuéntanos qué pasó sin contenido explícito "
    "y la revisaremos igual."
)


@router.post(
    "/incidents/{incident_id}/appeal",
    response_model=MyIncidentOut,
    dependencies=[Depends(_appeal_rate_limiter)],
)
def appeal_incident(
    incident_id: int, payload: AppealIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Apela un cierre propio, una vez. El texto pasa el filtro de entrada, pero lo que no
    pase solo da un 422: apelar no cierra ni cuenta nada."""
    text = (payload.text or "").strip()
    if text and moderation.check_user_text(text):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": APPEAL_TEXT_REJECTED, "code": "appeal_text_rejected"},
        )
    try:
        incident = conduct_service.appeal(db, user, incident_id, text or None)
    except conduct_service.ReviewError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc), "code": exc.code})
    return incident_service.my_incident_out(db, incident)
