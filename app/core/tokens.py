"""Emisión y validación de JWT.

Dos tokens con papeles distintos:

- **Access**, corto y sin estado. Se valida solo con la firma, sin tocar la base de
  datos, que es lo que hace barata cada petición.
- **Refresh**, largo y **con estado**: su `jti` vive en la tabla `refresh_tokens`. Sin
  esa tabla, cerrar sesión no podría revocar nada y un token robado valdría hasta que
  caducara.

El `type` va en las claims a propósito: sin él, un refresh serviría como access y
duraría treinta días en vez de treinta minutos.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings

ALGORITHM = "HS256"
ACCESS = "access"
REFRESH = "refresh"


class TokenError(Exception):
    """Token inválido, caducado o del tipo equivocado."""


@dataclass(frozen=True)
class IssuedToken:
    token: str
    jti: str
    expires_at: datetime


def _issue(user_id: str, token_type: str, minutes: int) -> IssuedToken:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=minutes)
    jti = str(uuid.uuid4())
    payload = {
        "sub": user_id,
        "type": token_type,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return IssuedToken(
        token=jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGORITHM),
        jti=jti,
        expires_at=expires,
    )


def issue_access(user_id: str) -> IssuedToken:
    return _issue(user_id, ACCESS, settings.ACCESS_TOKEN_MINUTES)


def issue_refresh(user_id: str) -> IssuedToken:
    return _issue(user_id, REFRESH, settings.REFRESH_TOKEN_MINUTES)


def decode(token: str, expected_type: str) -> dict:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise TokenError("El token ha caducado")
    except jwt.InvalidTokenError:
        raise TokenError("Token inválido")

    if payload.get("type") != expected_type:
        # Un refresh usado como access duraría treinta días en vez de treinta minutos.
        raise TokenError("Tipo de token incorrecto")
    if not payload.get("sub") or not payload.get("jti"):
        raise TokenError("Token incompleto")
    return payload
