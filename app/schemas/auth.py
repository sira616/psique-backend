from datetime import datetime

from pydantic import BaseModel, Field

from app.core.passwords import MAX_LENGTH, MIN_LENGTH
from app.core.config import settings
from app.models.user import User
from app.services import conduct_service


class UserRegister(BaseModel):
    """Solo lo imprescindible: cualquier campo que el cliente fije aquí es un campo que
    el cliente puede falsear."""

    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=MIN_LENGTH, max_length=MAX_LENGTH)
    display_name: str | None = Field(default=None, max_length=64)
    # Las dos casillas del registro. bool y no Literal[True]: la ruta da un 422 con un texto
    # que se pueda enseñar tal cual.
    accept_terms: bool = False
    min_age_confirmed: bool = False


class LoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=MAX_LENGTH)


class AuthUserOut(BaseModel):
    id: str
    username: str
    displayName: str
    # Identidad pública (para enlazar a /perfil/<handle>); `username` sigue siendo el login.
    handle: str
    isDev: bool
    adultConfirmed: bool
    # Solo si sigue vigente: una restricción pasada sale como null.
    restrictedUntil: datetime | None
    # Versión vigente de términos y privacidad, y si la cuenta la tiene aceptada. Con False
    # el cliente pide aceptarla (`POST /api/me/accept-terms`) antes de seguir.
    termsVersion: str
    termsAccepted: bool

    @classmethod
    def from_user(cls, user: User) -> "AuthUserOut":
        return cls(
            id=user.id,
            username=user.username,
            displayName=user.display_name,
            handle=user.handle,
            isDev=user.is_dev,
            adultConfirmed=conduct_service.adult_confirmed(user),
            restrictedUntil=conduct_service.restricted_until(user),
            termsVersion=settings.TERMS_VERSION,
            termsAccepted=user.terms_version == settings.TERMS_VERSION,
        )


class SessionOut(BaseModel):
    """El refresh no viaja en el cuerpo: va en una cookie httpOnly que JavaScript no lee."""

    access_token: str
    user: AuthUserOut
