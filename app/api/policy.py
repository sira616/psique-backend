"""Respuestas 403/422 de la política de contenido, compartidas por las rutas.

Todas llevan `code` para que el frontend decida qué enseñar sin leer el texto.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.models.story import Story
from app.models.user import User
from app.services import conduct_service, story_service
from app.story.content_policy import REDIRECT_MESSAGE, STORY_CLOSED_MESSAGE

ADULT_REQUIRED = "Este libro es +18. Para abrirlo tienes que confirmar que eres mayor de edad."


def _json(status_code: int, content: dict) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=jsonable_encoder(content))


def account_restricted_response(until: datetime) -> JSONResponse:
    return _json(
        status.HTTP_403_FORBIDDEN,
        {
            "detail": (
                "Tu cuenta tiene la lectura en pausa por cerrar varias partidas por incumplir las "
                "normas. Podrás volver a empezar, continuar o releer libros cuando termine."
            ),
            "code": "account_restricted",
            "restrictedUntil": until,
        },
    )


def adult_required_response() -> JSONResponse:
    return _json(status.HTTP_403_FORBIDDEN, {"detail": ADULT_REQUIRED, "code": "adult_required"})


def story_closed_response(story: Story, restricted_until: datetime | None = None) -> JSONResponse:
    return _json(
        status.HTTP_403_FORBIDDEN,
        {
            "detail": STORY_CLOSED_MESSAGE,
            "code": "story_closed",
            "closedAt": story.closed_at,
            "closedReason": story.closed_reason,
            "restrictedUntil": restricted_until,
        },
    )


def content_redirected_response(reply: str) -> JSONResponse:
    return _json(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        {"detail": REDIRECT_MESSAGE, "code": "content_redirected", "reply": reply},
    )


def reading_gate(db: Session, user: User, character_id: str) -> JSONResponse | None:
    """Lo que impide empezar, continuar o releer un libro, en orden de gravedad."""
    until = conduct_service.restricted_until(user)
    if until is not None:
        return account_restricted_response(until)
    if story_service.is_adult_book(db, character_id) and not conduct_service.adult_confirmed(user):
        return adult_required_response()
    return None
