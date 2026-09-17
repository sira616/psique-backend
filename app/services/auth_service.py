"""Registro y sesiones.

Los errores no distinguen "no existe" de "contraseña incorrecta": decir cuál falla
convierte el formulario en un buscador de cuentas.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from app.core import passwords, tokens
from app.core.config import settings
from app.models.auth import RefreshToken
from app.models.user import User
from app.services import economy_service, profile_service

USERNAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{1,30})[a-z0-9]$")
USERNAME_HELP = (
    "El usuario va de 3 a 32 caracteres: letras, números, punto, guion o guion bajo, "
    "empezando y acabando por letra o número."
)


class AuthError(Exception):
    """Fallo esperado, con mensaje apto para enseñar a quien lo provocó."""


def normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if not USERNAME_PATTERN.match(normalized):
        raise AuthError(USERNAME_HELP)
    return normalized


def create_user(db: DbSession, username: str, password: str, display_name: str | None) -> User:
    normalized = normalize_username(username)
    if db.scalar(select(User).where(User.username == normalized)):
        raise AuthError("Ese nombre de usuario ya está cogido.")

    user = User(
        id=str(uuid.uuid4()),
        username=normalized,
        password_hash=passwords.hash_password(password),
        display_name=(display_name or username).strip()[:64],
        handle=profile_service.unique_handle(db, normalized),
    )
    db.add(user)
    db.flush()
    # En la misma transacción que la cuenta: no puede existir una sin su bienvenida.
    economy_service.credit(db, user.id, settings.WELCOME_OBOLOS, "bienvenida")
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: DbSession, login: str, password: str) -> User:
    user = db.scalar(select(User).where(User.username == login.strip().lower()))
    if user is None:
        # Mismo coste de Argon2 en las dos ramas: si no, el cronómetro delata qué
        # cuentas existen aunque el mensaje sea idéntico.
        passwords.verify_dummy(password)
        raise AuthError("Usuario o contraseña incorrectos.")
    if not passwords.verify(password, user.password_hash):
        raise AuthError("Usuario o contraseña incorrectos.")
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
        db.commit()
    return user


def _store_refresh(db: DbSession, user_id: str, issued: tokens.IssuedToken) -> None:
    db.add(
        RefreshToken(
            jti=issued.jti, user_id=user_id, expires_at=issued.expires_at.replace(tzinfo=None)
        )
    )


def issue_session(db: DbSession, user: User) -> tuple[str, str]:
    access = tokens.issue_access(user.id)
    refresh = tokens.issue_refresh(user.id)
    _store_refresh(db, user.id, refresh)
    db.commit()
    return access.token, refresh.token


def rotate_session(db: DbSession, refresh_token: str) -> tuple[User, str, str]:
    """Canjea un refresh por una pareja nueva y anula el anterior: sin rotación, uno
    robado serviría treinta días en paralelo a la sesión legítima."""
    try:
        payload = tokens.decode(refresh_token, tokens.REFRESH)
    except tokens.TokenError as exc:
        raise AuthError(str(exc))

    row = db.scalar(select(RefreshToken).where(RefreshToken.jti == payload["jti"]))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if row is None or row.revoked or row.expires_at < now:
        raise AuthError("Sesión caducada o revocada. Vuelve a entrar.")
    user = db.get(User, payload["sub"])
    if user is None:
        raise AuthError("Sesión caducada o revocada. Vuelve a entrar.")

    access = tokens.issue_access(user.id)
    refresh = tokens.issue_refresh(user.id)
    row.revoked = True
    row.replaced_by = refresh.jti
    _store_refresh(db, user.id, refresh)
    db.commit()
    return user, access.token, refresh.token


def revoke(db: DbSession, refresh_token: str) -> None:
    """Un token ya inválido no es un error: el objetivo era que dejara de servir."""
    try:
        payload = tokens.decode(refresh_token, tokens.REFRESH)
    except tokens.TokenError:
        return
    db.execute(update(RefreshToken).where(RefreshToken.jti == payload["jti"]).values(revoked=True))
    db.commit()
